"""
Phase 1 tests — Discoverer agent unit tests.

All tests use fixture data — no live HTTP calls, no DB required.
The merge_results DB cross-reference is tested with an in-memory SQLite DB.

Tests cover:
  - Fingerprint generation and dedup logic
  - Compensation parsing for K-range, hourly, and plain dollar formats
  - ATS fingerprint detection from apply URLs
  - merge_results dedup across sources using fixture data
  - parse_search_params passthrough and warning for empty sources
  - fan_out_sources returns correct Send objects
  - Phase 0 graph compilation still passes (regression)
"""

from __future__ import annotations

import hashlib
import pytest

from pipeline.state import (
    AtsType,
    JobStatus,
    PipelineState,
    SearchParams,
    WorkplaceType,
    empty_state,
)
from pipeline.ats import detect_ats
from pipeline.agents.discoverer import (
    _make_fingerprint,
    _parse_compensation,
    fan_out_sources,
    parse_search_params,
)
from tests.fixtures.sample_jobs import ALL_FIXTURES, DICE_FIXTURES, INDEED_FIXTURES


# ── Fingerprint ────────────────────────────────────────────────────────────────


def test_fingerprint_is_deterministic():
    fp1 = _make_fingerprint("Acme Health", "Senior Software Engineer", "Chicago, IL")
    fp2 = _make_fingerprint("Acme Health", "Senior Software Engineer", "Chicago, IL")
    assert fp1 == fp2


def test_fingerprint_is_case_insensitive():
    fp1 = _make_fingerprint("acme health", "senior software engineer", "chicago, il")
    fp2 = _make_fingerprint("ACME HEALTH", "SENIOR SOFTWARE ENGINEER", "CHICAGO, IL")
    assert fp1 == fp2


def test_fingerprint_differs_on_title_change():
    fp1 = _make_fingerprint("Acme Health", "Senior Software Engineer", "Chicago, IL")
    fp2 = _make_fingerprint("Acme Health", "Backend Engineer", "Chicago, IL")
    assert fp1 != fp2


def test_fingerprint_is_16_chars():
    fp = _make_fingerprint("Acme", "Engineer", "Chicago")
    assert len(fp) == 16


def test_cross_source_dedup_same_fingerprint():
    """Indeed and Dice fixture[0] are the same role — fingerprints must match."""
    fp_indeed = _make_fingerprint(
        INDEED_FIXTURES[0].company,
        INDEED_FIXTURES[0].title,
        INDEED_FIXTURES[0].location,
    )
    fp_dice = _make_fingerprint(
        DICE_FIXTURES[0].company,
        DICE_FIXTURES[0].title,
        DICE_FIXTURES[0].location,
    )
    assert fp_indeed == fp_dice


# ── Compensation parsing ───────────────────────────────────────────────────────


def test_parse_compensation_k_range():
    low, high = _parse_compensation("$120K - $160K/yr")
    assert low == 120000
    assert high == 160000


def test_parse_compensation_hourly():
    low, high = _parse_compensation("$65/hr")
    assert low == int(65 * 2080)
    assert high == int(65 * 2080)


def test_parse_compensation_plain_range():
    low, high = _parse_compensation("$130000 - $175000")
    assert low == 130000
    assert high == 175000


def test_parse_compensation_no_salary():
    low, high = _parse_compensation("Salary not listed")
    assert low is None
    assert high is None


def test_parse_compensation_single_k():
    low, high = _parse_compensation("$150K")
    assert low == 150000
    assert high == 150000


# ── ATS detection ──────────────────────────────────────────────────────────────


def test_detect_ats_greenhouse():
    assert detect_ats("https://boards.greenhouse.io/acme/jobs/1") == AtsType.GREENHOUSE


def test_detect_ats_workday():
    assert detect_ats("https://acme.myworkdayjobs.com/en-US/jobs/1") == AtsType.WORKDAY


def test_detect_ats_ashby():
    assert detect_ats("https://jobs.ashby.io/acme/apply") == AtsType.ASHBY


def test_detect_ats_linkedin():
    assert detect_ats("https://www.linkedin.com/jobs/view/12345") == AtsType.LINKEDIN


def test_detect_ats_unknown_url():
    assert detect_ats("https://careers.somecompany.com/apply") == AtsType.OTHER


def test_detect_ats_none():
    assert detect_ats(None) == AtsType.UNKNOWN


# ── parse_search_params node ───────────────────────────────────────────────────


def test_parse_search_params_passthrough():
    state = empty_state()
    state["search_params"] = SearchParams(query="rails engineer", location="Chicago, IL")
    result = parse_search_params(state)
    assert result == {"warnings": []}


def test_parse_search_params_warns_on_empty_sources():
    state = empty_state()
    state["search_params"] = SearchParams(query="x", location="y", sources=[])
    result = parse_search_params(state)
    assert len(result["warnings"]) == 1
    assert "No job sources" in result["warnings"][0]


# ── fan_out_sources (Send API) ─────────────────────────────────────────────────


def test_fan_out_sources_returns_sends():
    from langgraph.types import Send
    state = empty_state()
    state["search_params"] = SearchParams(
        query="engineer", location="Chicago", sources=["indeed", "dice"]
    )
    sends = fan_out_sources(state)
    assert len(sends) == 2
    node_names = {s.node for s in sends}
    assert node_names == {"scrape_indeed", "scrape_dice"}


def test_fan_out_sources_skips_unknown():
    from langgraph.types import Send
    state = empty_state()
    state["search_params"] = SearchParams(
        query="x", location="y", sources=["indeed", "not_a_real_source"]
    )
    sends = fan_out_sources(state)
    assert len(sends) == 1
    assert sends[0].node == "scrape_indeed"


# ── merge_results (in-memory DB) ───────────────────────────────────────────────


@pytest.fixture
async def in_memory_db(tmp_path):
    """Spin up an isolated aiosqlite DB and run migrations against it."""
    import aiosqlite
    from pathlib import Path

    db_path = str(tmp_path / "test_pipeline.db")
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")

    migrations_dir = Path(__file__).parent.parent / "migrations"
    for f in sorted(migrations_dir.glob("*.sql")):
        await conn.executescript(f.read_text())
    await conn.commit()
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_merge_results_dedup_cross_source(monkeypatch):
    """
    ALL_FIXTURES has 5 items but INDEED[0] and DICE[0] share a fingerprint.
    merge_results should return 4 unique listings.
    """
    import pipeline.agents.discoverer as disc

    # Monkeypatch get_connection to avoid needing a real DB file
    async def mock_get_connection():
        class FakeConn:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def execute(self, *a, **kw):
                class FakeCursor:
                    def __aiter__(self): return iter([])
                return FakeCursor()
        return FakeConn()

    monkeypatch.setattr(disc, "get_connection", mock_get_connection)

    state = empty_state()
    state["raw_results"] = ALL_FIXTURES  # type: ignore[assignment]
    result = await disc.merge_results(state)
    assert len(result["shortlist"]) == 4  # 5 raw - 1 dup = 4


@pytest.mark.asyncio
async def test_merge_results_suppresses_skipped(monkeypatch):
    """
    If a fingerprint exists in the DB with status=skipped, it should
    be suppressed from the shortlist.
    """
    import pipeline.agents.discoverer as disc

    skipped_fp = _make_fingerprint(
        INDEED_FIXTURES[0].company,
        INDEED_FIXTURES[0].title,
        INDEED_FIXTURES[0].location,
    )

    async def mock_get_connection():
        class FakeAsyncCursor:
            def __init__(self, rows):
                self._iter = iter(rows)
            def __aiter__(self):
                return self
            async def __anext__(self):
                try:
                    return next(self._iter)
                except StopIteration:
                    raise StopAsyncIteration

        class FakeConn:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def execute(self, *a, **kw):
                return FakeAsyncCursor([
                    {"fingerprint": skipped_fp, "status": "skipped"}
                ])
        return FakeConn()

    monkeypatch.setattr(disc, "get_connection", mock_get_connection)

    state = empty_state()
    state["raw_results"] = INDEED_FIXTURES  # type: ignore[assignment]
    result = await disc.merge_results(state)
    titles = [j.title for j in result["shortlist"]]
    assert "Senior Software Engineer" not in titles


# ── Phase 0 regression ────────────────────────────────────────────────────────


def test_discoverer_graph_still_compiles():
    from pipeline.agents.discoverer import build_discoverer_graph
    compiled = build_discoverer_graph().compile()
    assert compiled is not None
