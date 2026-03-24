"""
Discoverer agent tests — fingerprinting, compensation parsing, merge_results,
HTML card parsers, Playwright auth helpers, and scraper node behavior.

All tests use fixture data. No live HTTP calls, no real browser.
Playwright is never imported directly — scraper nodes are tested via
monkeypatched async context managers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.state import AtsType, JobStatus, SearchParams, WorkplaceType, empty_state
from pipeline.agents.discoverer import (
    _make_fingerprint,
    _parse_compensation,
    fan_out_sources,
    parse_search_params,
)
from tests.fixtures.sample_jobs import ALL_FIXTURES, DICE_FIXTURES, INDEED_FIXTURES

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(filename: str) -> str:
    return (FIXTURES_DIR / filename).read_text()


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
    assert {s.node for s in sends} == {"scrape_indeed", "scrape_dice"}


def test_fan_out_sources_skips_unknown():
    state = empty_state()
    state["search_params"] = SearchParams(
        query="x", location="y", sources=["indeed", "not_a_real_source"]
    )
    sends = fan_out_sources(state)
    assert len(sends) == 1
    assert sends[0].node == "scrape_indeed"


# ── merge_results (async, mocked DB) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_merge_results_dedup_cross_source(monkeypatch):
    """
    ALL_FIXTURES has 5 items but INDEED[0] and DICE[0] share a fingerprint.
    merge_results should return 4 unique listings.
    """
    import pipeline.agents.discoverer as disc

    async def mock_get_connection():
        class FakeCursor:
            def __aiter__(self): return iter([])
        class FakeConn:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def execute(self, *a, **kw): return FakeCursor()
        return FakeConn()

    monkeypatch.setattr(disc, "get_connection", mock_get_connection)
    state = empty_state()
    state["raw_results"] = ALL_FIXTURES  # type: ignore[assignment]
    result = await disc.merge_results(state)
    assert len(result["shortlist"]) == 4


@pytest.mark.asyncio
async def test_merge_results_suppresses_skipped(monkeypatch):
    """Listings with status=skipped in the DB are suppressed from the shortlist."""
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
            def __aiter__(self): return self
            async def __anext__(self):
                try:
                    return next(self._iter)
                except StopIteration:
                    raise StopAsyncIteration

        class FakeConn:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def execute(self, *a, **kw):
                return FakeAsyncCursor([{"fingerprint": skipped_fp, "status": "skipped"}])
        return FakeConn()

    monkeypatch.setattr(disc, "get_connection", mock_get_connection)
    state = empty_state()
    state["raw_results"] = INDEED_FIXTURES  # type: ignore[assignment]
    result = await disc.merge_results(state)
    assert "Senior Software Engineer" not in [j.title for j in result["shortlist"]]


# ── Auth path helpers ──────────────────────────────────────────────────────────


def test_get_auth_path_linkedin():
    from pipeline.agents.discoverer import _get_auth_path
    path = _get_auth_path("linkedin")
    assert path.name == "linkedin.json"
    assert "playwright" in str(path)
    assert ".auth" in str(path)


def test_get_auth_path_ziprecruiter():
    from pipeline.agents.discoverer import _get_auth_path
    path = _get_auth_path("ziprecruiter")
    assert path.name == "ziprecruiter.json"
    assert "playwright" in str(path)


# ── Login redirect detection ───────────────────────────────────────────────────


def test_is_login_redirect_detects_linkedin_login():
    from pipeline.agents.discoverer import _is_login_redirect
    assert _is_login_redirect("https://www.linkedin.com/login", "linkedin") is True


def test_is_login_redirect_safe_url():
    from pipeline.agents.discoverer import _is_login_redirect
    assert _is_login_redirect("https://www.linkedin.com/jobs/search/", "linkedin") is False


def test_is_login_redirect_detects_ziprecruiter_login():
    from pipeline.agents.discoverer import _is_login_redirect
    assert _is_login_redirect("https://www.ziprecruiter.com/login", "ziprecruiter") is True


def test_is_login_redirect_ziprecruiter_safe_url():
    from pipeline.agents.discoverer import _is_login_redirect
    assert _is_login_redirect("https://www.ziprecruiter.com/jobs-search", "ziprecruiter") is False


# ── LinkedIn card parser ───────────────────────────────────────────────────────


def test_parse_linkedin_cards_happy_path():
    from pipeline.agents.discoverer import _parse_linkedin_cards
    results = _parse_linkedin_cards(_load_fixture("linkedin_job_cards.html"))
    assert len(results) == 2
    assert results[0].title == "Senior Rails Engineer"
    assert results[0].company == "Acme Health"
    assert results[0].source == "linkedin"
    assert "linkedin.com" in results[0].source_url


def test_parse_linkedin_cards_empty_page():
    from pipeline.agents.discoverer import _parse_linkedin_cards
    assert _parse_linkedin_cards("<html><body></body></html>") == []


def test_parse_linkedin_cards_missing_company_skipped_gracefully():
    from pipeline.agents.discoverer import _parse_linkedin_cards
    results = _parse_linkedin_cards(_load_fixture("linkedin_job_cards.html"))
    assert "Backend Engineer" not in [r.title for r in results]


def test_parse_linkedin_cards_salary_extracted():
    from pipeline.agents.discoverer import _parse_linkedin_cards
    results = _parse_linkedin_cards(_load_fixture("linkedin_job_cards.html"))
    assert results[0].compensation_low == 140000
    assert results[0].compensation_high == 170000


# ── ZipRecruiter card parser ───────────────────────────────────────────────────


def test_parse_ziprecruiter_cards_happy_path():
    from pipeline.agents.discoverer import _parse_ziprecruiter_cards
    results = _parse_ziprecruiter_cards(_load_fixture("ziprecruiter_job_cards.html"))
    assert len(results) == 2
    assert results[0].title == "Senior Software Engineer"
    assert results[0].company == "Acme Corp"
    assert results[0].source == "ziprecruiter"


def test_parse_ziprecruiter_cards_empty_page():
    from pipeline.agents.discoverer import _parse_ziprecruiter_cards
    assert _parse_ziprecruiter_cards("<html><body></body></html>") == []


def test_parse_ziprecruiter_cards_salary_range():
    from pipeline.agents.discoverer import _parse_ziprecruiter_cards
    results = _parse_ziprecruiter_cards(_load_fixture("ziprecruiter_job_cards.html"))
    assert results[0].compensation_low == 120000
    assert results[0].compensation_high == 150000


# ── Scraper node behavior — missing auth ──────────────────────────────────────


@pytest.mark.asyncio
async def test_scrape_linkedin_missing_auth_returns_empty(monkeypatch, tmp_path):
    import pipeline.agents.discoverer as disc
    monkeypatch.setattr(disc, "_get_auth_path", lambda p: tmp_path / "nonexistent.json")
    state = empty_state()
    state["search_params"] = SearchParams(query="engineer", location="Chicago, IL")
    assert await disc.scrape_linkedin(state) == {"raw_results": []}


@pytest.mark.asyncio
async def test_scrape_ziprecruiter_missing_auth_returns_empty(monkeypatch, tmp_path):
    import pipeline.agents.discoverer as disc
    monkeypatch.setattr(disc, "_get_auth_path", lambda p: tmp_path / "nonexistent.json")
    state = empty_state()
    state["search_params"] = SearchParams(query="engineer", location="Chicago, IL")
    assert await disc.scrape_ziprecruiter(state) == {"raw_results": []}


# ── Scraper node behavior — stale session ─────────────────────────────────────


class _FakePage:
    def __init__(self, redirect_url: str):
        self.url = redirect_url

    async def goto(self, url, **kwargs): pass
    async def wait_for_selector(self, *a, **kw): pass
    async def content(self): return "<html></html>"


class _FakeContext:
    def __init__(self, page): self._page = page
    async def new_page(self): return self._page
    async def close(self): pass


class _FakeBrowser:
    def __init__(self, page): self._page = page
    async def new_context(self, **kwargs): return _FakeContext(self._page)
    async def close(self): pass


class _FakePlaywright:
    def __init__(self, page):
        self.chromium = _FakeChromium(page)

    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass


class _FakeChromium:
    def __init__(self, page): self._page = page
    async def launch(self, **kwargs): return _FakeBrowser(self._page)


@pytest.mark.asyncio
async def test_scrape_linkedin_stale_session_returns_empty(monkeypatch, tmp_path):
    import pipeline.agents.discoverer as disc
    auth_file = tmp_path / "linkedin.json"
    auth_file.write_text("{}")
    monkeypatch.setattr(disc, "_get_auth_path", lambda p: auth_file)
    monkeypatch.setattr(
        disc, "async_playwright",
        lambda: _FakePlaywright(_FakePage("https://www.linkedin.com/login"))
    )
    state = empty_state()
    state["search_params"] = SearchParams(query="engineer", location="Chicago, IL")
    assert await disc.scrape_linkedin(state) == {"raw_results": []}


@pytest.mark.asyncio
async def test_scrape_ziprecruiter_stale_session_returns_empty(monkeypatch, tmp_path):
    import pipeline.agents.discoverer as disc
    auth_file = tmp_path / "ziprecruiter.json"
    auth_file.write_text("{}")
    monkeypatch.setattr(disc, "_get_auth_path", lambda p: auth_file)
    monkeypatch.setattr(
        disc, "async_playwright",
        lambda: _FakePlaywright(_FakePage("https://www.ziprecruiter.com/login"))
    )
    state = empty_state()
    state["search_params"] = SearchParams(query="engineer", location="Chicago, IL")
    assert await disc.scrape_ziprecruiter(state) == {"raw_results": []}
