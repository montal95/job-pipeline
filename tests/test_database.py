"""
Database module tests — get_connection context manager, run_migrations.

All tests use a real SQLite in-memory or temp-file DB.
No mocking of aiosqlite — we test the actual context manager contract.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


# ── get_connection ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_connection_returns_async_context_manager(tmp_path, monkeypatch):
    """get_connection() must be synchronous and return an async context manager."""
    import inspect
    from pipeline import database as db_module
    monkeypatch.setattr(db_module.settings, "app_db_path", str(tmp_path / "test.db"))

    cm = db_module.get_connection()
    assert not inspect.iscoroutine(cm), "get_connection() must not be async (no await)"
    assert hasattr(cm, "__aenter__") and hasattr(cm, "__aexit__")


@pytest.mark.asyncio
async def test_get_connection_opens_and_closes(tmp_path, monkeypatch):
    """Connection is open inside the block and closed after __aexit__."""
    import aiosqlite
    from pipeline import database as db_module
    monkeypatch.setattr(db_module.settings, "app_db_path", str(tmp_path / "test.db"))

    async with db_module.get_connection() as conn:
        assert isinstance(conn, aiosqlite.Connection)
        # Should be able to execute a query
        await conn.execute("SELECT 1")

    # After exiting, further queries should fail
    with pytest.raises(Exception):
        await conn.execute("SELECT 1")


@pytest.mark.asyncio
async def test_get_connection_enables_foreign_keys(tmp_path, monkeypatch):
    """PRAGMA foreign_keys should be ON after get_connection opens."""
    from pipeline import database as db_module
    monkeypatch.setattr(db_module.settings, "app_db_path", str(tmp_path / "test.db"))

    async with db_module.get_connection() as conn:
        cursor = await conn.execute("PRAGMA foreign_keys")
        row = await cursor.fetchone()
        assert row[0] == 1


@pytest.mark.asyncio
async def test_get_connection_creates_parent_directory(tmp_path, monkeypatch):
    """get_connection() creates the parent directory if it doesn't exist."""
    nested = tmp_path / "a" / "b" / "c" / "test.db"
    from pipeline import database as db_module
    monkeypatch.setattr(db_module.settings, "app_db_path", str(nested))

    async with db_module.get_connection() as conn:
        await conn.execute("SELECT 1")

    assert nested.parent.exists()


@pytest.mark.asyncio
async def test_get_connection_multiple_sequential_calls(tmp_path, monkeypatch):
    """Two sequential get_connection() calls must both work independently."""
    from pipeline import database as db_module
    monkeypatch.setattr(db_module.settings, "app_db_path", str(tmp_path / "test.db"))

    async with db_module.get_connection() as conn1:
        await conn1.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")
        await conn1.commit()

    async with db_module.get_connection() as conn2:
        await conn2.execute("INSERT INTO t (id) VALUES (1)")
        await conn2.commit()

    # Verify data persisted across connections
    async with db_module.get_connection() as conn3:
        cursor = await conn3.execute("SELECT COUNT(*) FROM t")
        row = await cursor.fetchone()
        assert row[0] == 1
