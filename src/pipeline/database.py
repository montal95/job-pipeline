"""
Database module: connection management and migration runner.

Two separate databases:
  - App DB (pipeline.db): job search business data — jobs, submissions, search runs, company cache
  - Checkpoint DB (checkpoints.db): LangGraph execution state (managed by SqliteSaver)

The app DB is managed here. The checkpoint DB is managed by LangGraph.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite

from pipeline.config import settings

MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "migrations"


async def get_connection() -> aiosqlite.Connection:
    """Return an open connection to the app DB with foreign keys enabled."""
    Path(settings.app_db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(settings.app_db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    return conn


async def run_migrations() -> None:
    """Apply all pending SQL migrations in order."""
    conn = await get_connection()
    try:
        # Track applied migrations in a simple meta table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS _migrations (
                filename TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        await conn.commit()

        applied = {
            row[0]
            async for row in await conn.execute("SELECT filename FROM _migrations")
        }

        migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        for migration_file in migration_files:
            if migration_file.name in applied:
                continue
            sql = migration_file.read_text()
            await conn.executescript(sql)
            await conn.execute(
                "INSERT INTO _migrations (filename, applied_at) VALUES (?, datetime('now'))",
                (migration_file.name,),
            )
            await conn.commit()
            print(f"[db] Applied migration: {migration_file.name}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run_migrations())
