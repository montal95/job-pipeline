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


def get_connection() -> aiosqlite.Connection:
    """
    Return an aiosqlite connection context manager for the app DB.

    Usage:
        async with get_connection() as conn:
            await conn.execute(...)

    The connection is opened, configured, and closed automatically.
    Do NOT await this function — use it directly as an async context manager.
    """
    Path(settings.app_db_path).parent.mkdir(parents=True, exist_ok=True)

    class _ManagedConn:
        async def __aenter__(self) -> aiosqlite.Connection:
            self._conn = await aiosqlite.connect(settings.app_db_path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA foreign_keys = ON")
            return self._conn

        async def __aexit__(self, *args) -> None:
            await self._conn.close()

    return _ManagedConn()  # type: ignore[return-value]


async def run_migrations() -> None:
    """Apply all pending SQL migrations in order."""
    async with get_connection() as conn:
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
            try:
                await conn.executescript(sql)
            except Exception as exc:
                # ALTER TABLE ADD COLUMN raises OperationalError("duplicate column name: X")
                # if a previous run applied the DDL but crashed before recording it in
                # _migrations. The columns already exist — safe to record and continue.
                if "duplicate column name" in str(exc).lower():
                    print(
                        f"[db] Warning: {migration_file.name} — columns already exist "
                        f"(prior partial run?). Recording as applied and continuing."
                    )
                else:
                    raise
            await conn.execute(
                "INSERT INTO _migrations (filename, applied_at) VALUES (?, datetime('now'))",
                (migration_file.name,),
            )
            await conn.commit()
            print(f"[db] Applied migration: {migration_file.name}")


if __name__ == "__main__":
    asyncio.run(run_migrations())
