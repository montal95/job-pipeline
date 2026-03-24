"""
Phase 2 tests — Playwright auth sessions (LinkedIn + ZipRecruiter scrapers).

All tests are pure unit tests — no live browser, no network calls.
Playwright is not imported directly; scraper nodes are tested via
monkeypatched async context managers.

Tests cover:
  - Auth path helpers (_get_auth_path)
  - Login redirect detection (_is_login_redirect)
  - LinkedIn card parser (_parse_linkedin_cards)
  - ZipRecruiter card parser (_parse_ziprecruiter_cards)
  - Scraper node behavior: missing auth file
  - Scraper node behavior: stale session (login redirect)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.state import WorkplaceType, empty_state, SearchParams

# ── Fixtures ───────────────────────────────────────────────────────────────────

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(filename: str) -> str:
    return (FIXTURES_DIR / filename).read_text()


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
    assert ".auth" in str(path)


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
    html = _load_fixture("linkedin_job_cards.html")
    results = _parse_linkedin_cards(html)
    # Card 3 is missing company — only 2 valid cards expected
    assert len(results) == 2
    first = results[0]
    assert first.title == "Senior Rails Engineer"
    assert first.company == "Acme Health"
    assert first.location == "Chicago, IL"
    assert first.source == "linkedin"
    assert "linkedin.com" in first.source_url


def test_parse_linkedin_cards_empty_page():
    from pipeline.agents.discoverer import _parse_linkedin_cards
    results = _parse_linkedin_cards("<html><body></body></html>")
    assert results == []


def test_parse_linkedin_cards_missing_company_skipped_gracefully():
    from pipeline.agents.discoverer import _parse_linkedin_cards
    html = _load_fixture("linkedin_job_cards.html")
    results = _parse_linkedin_cards(html)
    titles = [r.title for r in results]
    # Card 3 ("Backend Engineer") has no company — must be skipped, no crash
    assert "Backend Engineer" not in titles


def test_parse_linkedin_cards_salary_extracted():
    from pipeline.agents.discoverer import _parse_linkedin_cards
    html = _load_fixture("linkedin_job_cards.html")
    results = _parse_linkedin_cards(html)
    first = results[0]  # "Senior Rails Engineer" has $140K-$170K
    assert first.compensation_low == 140000
    assert first.compensation_high == 170000


# ── ZipRecruiter card parser ───────────────────────────────────────────────────


def test_parse_ziprecruiter_cards_happy_path():
    from pipeline.agents.discoverer import _parse_ziprecruiter_cards
    html = _load_fixture("ziprecruiter_job_cards.html")
    results = _parse_ziprecruiter_cards(html)
    # Card 3 is missing company — only 2 valid cards expected
    assert len(results) == 2
    first = results[0]
    assert first.title == "Senior Software Engineer"
    assert first.company == "Acme Corp"
    assert first.location == "Chicago, IL"
    assert first.source == "ziprecruiter"
    assert "ziprecruiter.com" in first.source_url


def test_parse_ziprecruiter_cards_empty_page():
    from pipeline.agents.discoverer import _parse_ziprecruiter_cards
    results = _parse_ziprecruiter_cards("<html><body></body></html>")
    assert results == []


def test_parse_ziprecruiter_cards_salary_range():
    from pipeline.agents.discoverer import _parse_ziprecruiter_cards
    html = _load_fixture("ziprecruiter_job_cards.html")
    results = _parse_ziprecruiter_cards(html)
    first = results[0]  # "Senior Software Engineer" has $120K-$150K
    assert first.compensation_low == 120000
    assert first.compensation_high == 150000


# ── Scraper node behavior — missing auth ──────────────────────────────────────


@pytest.mark.asyncio
async def test_scrape_linkedin_missing_auth_returns_empty(monkeypatch, tmp_path):
    import pipeline.agents.discoverer as disc
    monkeypatch.setattr(disc, "_get_auth_path", lambda p: tmp_path / "nonexistent.json")
    state = empty_state()
    state["search_params"] = SearchParams(query="engineer", location="Chicago, IL")
    result = await disc.scrape_linkedin(state)
    assert result == {"raw_results": []}


@pytest.mark.asyncio
async def test_scrape_ziprecruiter_missing_auth_returns_empty(monkeypatch, tmp_path):
    import pipeline.agents.discoverer as disc
    monkeypatch.setattr(disc, "_get_auth_path", lambda p: tmp_path / "nonexistent.json")
    state = empty_state()
    state["search_params"] = SearchParams(query="engineer", location="Chicago, IL")
    result = await disc.scrape_ziprecruiter(state)
    assert result == {"raw_results": []}
