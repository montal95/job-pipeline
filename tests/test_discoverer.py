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

from pipeline.state import AtsType, JobStatus, RawJobListing, SearchParams, WorkplaceType, empty_state
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


# ── _extract_salary_from_description ──────────────────────────────────────────


def test_extract_salary_from_description_base_salary_range():
    """LinkedIn 'Base salary range $202,300 - $238,000' pattern."""
    from pipeline.agents.discoverer import _extract_salary_from_description
    low, high = _extract_salary_from_description(
        "About the job\nBase salary range $202,300 - $238,000\nBenefits included."
    )
    assert low == 202300
    assert high == 238000


def test_extract_salary_from_description_k_notation():
    """Pay range using K notation: '$95K - $115K'."""
    from pipeline.agents.discoverer import _extract_salary_from_description
    low, high = _extract_salary_from_description(
        "Compensation\nPay range: $95K - $115K annually\nBonuses available."
    )
    assert low == 95000
    assert high == 115000


def test_extract_salary_from_description_no_salary():
    """Returns None, None when no salary is mentioned."""
    from pipeline.agents.discoverer import _extract_salary_from_description
    low, high = _extract_salary_from_description(
        "We are looking for a software engineer to join our team.\nGreat benefits."
    )
    assert low is None
    assert high is None


def test_extract_salary_from_description_empty_string():
    from pipeline.agents.discoverer import _extract_salary_from_description
    assert _extract_salary_from_description("") == (None, None)


def test_extract_salary_from_description_range_with_em_dash():
    """Handles em-dash separator as well as hyphen."""
    from pipeline.agents.discoverer import _extract_salary_from_description
    low, high = _extract_salary_from_description(
        "Salary: $120,000 – $160,000 per year"
    )
    assert low == 120000
    assert high == 160000


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

    def mock_get_connection():
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

    def mock_get_connection():
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


# ── LinkedIn card parser — authenticated DOM ──────────────────────────────────


def test_parse_linkedin_cards_auth_dom_happy_path():
    """Authenticated DOM (div.job-card-container) is parsed correctly."""
    from pipeline.agents.discoverer import _parse_linkedin_cards
    results = _parse_linkedin_cards(_load_fixture("linkedin_job_cards_auth.html"))
    assert len(results) == 2
    assert results[0].title == "Senior Rails Engineer"
    assert results[0].company == "Acme Health"
    assert results[0].source == "linkedin"


def test_parse_linkedin_cards_auth_dom_url_uses_data_job_id():
    """URL is constructed from data-job-id attribute, not from href."""
    from pipeline.agents.discoverer import _parse_linkedin_cards
    results = _parse_linkedin_cards(_load_fixture("linkedin_job_cards_auth.html"))
    assert results[0].source_url == "https://www.linkedin.com/jobs/view/auth-111111"
    assert results[1].source_url == "https://www.linkedin.com/jobs/view/auth-222222"


def test_parse_linkedin_cards_auth_dom_salary_extracted():
    from pipeline.agents.discoverer import _parse_linkedin_cards
    results = _parse_linkedin_cards(_load_fixture("linkedin_job_cards_auth.html"))
    assert results[0].compensation_low == 140000
    assert results[0].compensation_high == 170000


def test_parse_linkedin_cards_auth_dom_remote_detected():
    from pipeline.agents.discoverer import _parse_linkedin_cards
    from pipeline.state import WorkplaceType
    results = _parse_linkedin_cards(_load_fixture("linkedin_job_cards_auth.html"))
    assert results[1].workplace_type == WorkplaceType.REMOTE


def test_parse_linkedin_cards_auth_dom_missing_company_skipped():
    from pipeline.agents.discoverer import _parse_linkedin_cards
    results = _parse_linkedin_cards(_load_fixture("linkedin_job_cards_auth.html"))
    assert "Backend Engineer" not in [r.title for r in results]


def test_parse_linkedin_cards_auth_dom_takes_priority_over_public():
    """When both auth and public cards exist, auth path wins (returns auth results)."""
    from pipeline.agents.discoverer import _parse_linkedin_cards
    results = _parse_linkedin_cards(_load_fixture("linkedin_job_cards_auth.html"))
    assert all("/jobs/view/" in r.source_url for r in results)


# ── ZipRecruiter card parser ───────────────────────────────────────────────────
#
# The scraper switched from BS4 HTML parsing to Playwright JS extraction.
# Tests now target _transform_ziprecruiter_jobs() which is the pure transform
# function extracted from the scraper. The fixture is a list of dicts matching
# the shape returned by the page.evaluate() call in scrape_ziprecruiter:
#   { company, title, location (city · workplace_type), salary, url }

ZR_FIXTURE = [
    {
        "company": "YO IT CONSULTING",
        "title": "Ruby on Rails Developer - LLM",
        "location": "Chicago, IL · Remote",
        "salary": "$105.60K - $144.70K/yr",
        "url": "https://www.ziprecruiter.com/jobs/yo-it-consulting/ruby-on-rails",
    },
    {
        "company": "AppFolio",
        "title": "Sr. Software Engineer - Accounting",
        "location": "Chicago, IL · On-site",
        "salary": "$126K - $166K/yr",
        "url": "https://www.ziprecruiter.com/jobs/appfolio/sr-software-engineer",
    },
    {
        "company": "Halo",
        "title": "Software Engineer",
        "location": "Chicago, IL",
        "salary": "",
        "url": "https://www.ziprecruiter.com/jobs/halo/software-engineer",
    },
    {
        # Missing title — should be skipped
        "company": "BadCard Inc",
        "title": "",
        "location": "Chicago, IL",
        "salary": "",
        "url": "",
    },
]


def test_transform_ziprecruiter_jobs_happy_path():
    from pipeline.agents.discoverer import _transform_ziprecruiter_jobs
    results = _transform_ziprecruiter_jobs(ZR_FIXTURE)
    assert len(results) == 3  # BadCard skipped (no title)
    assert results[0].title == "Ruby on Rails Developer - LLM"
    assert results[0].company == "YO IT CONSULTING"
    assert results[0].source == "ziprecruiter"


def test_transform_ziprecruiter_jobs_salary_extracted():
    from pipeline.agents.discoverer import _transform_ziprecruiter_jobs
    results = _transform_ziprecruiter_jobs(ZR_FIXTURE)
    assert results[0].compensation_low == 105600
    assert results[0].compensation_high == 144700


def test_transform_ziprecruiter_jobs_location_splits_on_bullet():
    """Location 'Chicago, IL · Remote' should split — city goes to location, type to workplace."""
    from pipeline.agents.discoverer import _transform_ziprecruiter_jobs
    from pipeline.state import WorkplaceType
    results = _transform_ziprecruiter_jobs(ZR_FIXTURE)
    assert results[0].location == "Chicago, IL"
    assert results[0].workplace_type == WorkplaceType.REMOTE


def test_transform_ziprecruiter_jobs_onsite_detected():
    from pipeline.agents.discoverer import _transform_ziprecruiter_jobs
    from pipeline.state import WorkplaceType
    results = _transform_ziprecruiter_jobs(ZR_FIXTURE)
    assert results[1].workplace_type == WorkplaceType.ONSITE


def test_transform_ziprecruiter_jobs_no_salary_is_none():
    from pipeline.agents.discoverer import _transform_ziprecruiter_jobs
    results = _transform_ziprecruiter_jobs(ZR_FIXTURE)
    assert results[2].compensation_low is None


def test_transform_ziprecruiter_jobs_skips_empty_title():
    from pipeline.agents.discoverer import _transform_ziprecruiter_jobs
    results = _transform_ziprecruiter_jobs(ZR_FIXTURE)
    assert "BadCard Inc" not in [r.company for r in results]


def test_transform_ziprecruiter_jobs_empty_input():
    from pipeline.agents.discoverer import _transform_ziprecruiter_jobs
    assert _transform_ziprecruiter_jobs([]) == []


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
    """
    ZipRecruiter discoverer no longer requires auth — public search works.
    Auth infrastructure (_get_auth_path, save_auth.py) is retained for the
    submitter which needs a logged-in session to apply. This test verifies
    graceful return when Playwright is unavailable, consistent with scrape_dice.
    """
    import pipeline.agents.discoverer as disc
    monkeypatch.setattr(disc, "async_playwright", None)
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


# ── Dice scraper ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scrape_dice_no_playwright_returns_empty(monkeypatch):
    """scrape_dice returns empty gracefully when Playwright is unavailable."""
    import pipeline.agents.discoverer as disc
    monkeypatch.setattr(disc, "async_playwright", None)
    state = empty_state()
    state["search_params"] = SearchParams(query="engineer", location="Chicago, IL")
    assert await disc.scrape_dice(state) == {"raw_results": []}


# ── Built In scraper ───────────────────────────────────────────────────────────
#
# Card structure confirmed via Claude in Chrome DevTools inspection (March 2026):
#   [data-id="job-card"] lines: [company, title, date, workplace, location, salary?, level]
#   Job URL: card.querySelectorAll('a')[2].pathname → /job/{slug}/{id}
#   Salary format: "165K-195K Annually" or "95K/hr Hourly"

BUILTIN_FIXTURE = [
    {
        "company": "Doximity",
        "title": "Software Engineer (Ruby/Rails), Advertising Platform",
        "workplace": "In-Office or Remote",
        "location": "2 Locations",
        "salary": "165K-195K Annually",
        "url": "/job/software-engineer-ruby-rails-advertising-platform/8319205",
    },
    {
        "company": "Forward Financing",
        "title": "Principal Software Engineer (Ruby on Rails)",
        "workplace": "Remote",
        "location": "United States",  # vague — normalizes to blank
        "salary": "200K-230K Annually",
        "url": "/job/principal-software-engineer-ruby-on-rails/9999999",
    },
    {
        "company": "PayPal",
        "title": "Sr Software Engineer, Ruby on Rails",
        "workplace": "In-Office",
        "location": "Chicago, IL, USA",
        "salary": "131K-194K Annually",
        "url": "/job/sr-software-engineer-ruby-on-rails/8888888",
    },
    {
        "company": "G2",
        "title": "Software Engineer (Ruby on Rails)",
        "workplace": "Remote",
        "location": "US",
        "salary": "",
        "url": "/job/software-engineer-ruby-on-rails/7777777",
    },
    {
        # Missing title — should be skipped
        "company": "BadCo",
        "title": "",
        "workplace": "Remote",
        "location": "USA",
        "salary": "",
        "url": "",
    },
]


def test_transform_builtin_jobs_happy_path():
    from pipeline.agents.discoverer import _transform_builtin_jobs
    results = _transform_builtin_jobs(BUILTIN_FIXTURE)
    assert len(results) == 4  # BadCo skipped (no title)
    assert results[0].title == "Software Engineer (Ruby/Rails), Advertising Platform"
    assert results[0].company == "Doximity"
    assert results[0].source == "builtin"


def test_transform_builtin_jobs_url_is_absolute():
    from pipeline.agents.discoverer import _transform_builtin_jobs
    results = _transform_builtin_jobs(BUILTIN_FIXTURE)
    assert results[0].source_url == "https://builtin.com/job/software-engineer-ruby-rails-advertising-platform/8319205"


def test_transform_builtin_jobs_salary_extracted():
    from pipeline.agents.discoverer import _transform_builtin_jobs
    results = _transform_builtin_jobs(BUILTIN_FIXTURE)
    assert results[0].compensation_low == 165000
    assert results[0].compensation_high == 195000


def test_transform_builtin_jobs_remote_detected():
    from pipeline.agents.discoverer import _transform_builtin_jobs
    from pipeline.state import WorkplaceType
    results = _transform_builtin_jobs(BUILTIN_FIXTURE)
    assert results[1].workplace_type == WorkplaceType.REMOTE


def test_transform_builtin_jobs_onsite_detected():
    from pipeline.agents.discoverer import _transform_builtin_jobs
    from pipeline.state import WorkplaceType
    results = _transform_builtin_jobs(BUILTIN_FIXTURE)
    assert results[2].workplace_type == WorkplaceType.ONSITE


def test_transform_builtin_jobs_no_salary_is_none():
    from pipeline.agents.discoverer import _transform_builtin_jobs
    results = _transform_builtin_jobs(BUILTIN_FIXTURE)
    assert results[3].compensation_low is None


def test_transform_builtin_jobs_vague_location_normalized():
    """USA, US, United States, '2 Locations' should all normalize to blank."""
    from pipeline.agents.discoverer import _transform_builtin_jobs
    results = _transform_builtin_jobs(BUILTIN_FIXTURE)
    # Forward Financing has "United States" → should be blank
    ff = next(r for r in results if r.company == "Forward Financing")
    assert ff.location == ""
    # PayPal has "Chicago, IL, USA" → should be kept as-is
    pp = next(r for r in results if r.company == "PayPal")
    assert pp.location == "Chicago, IL, USA"


# ── Wellfound scraper ──────────────────────────────────────────────────────────
#
# Card structure confirmed via Chrome DevTools inspection (March 2026):
#   Company cards: .mb-6.w-full.rounded.border.border-gray-400.bg-white
#   Job divs within card: .min-h-[50px] (one per role)
#   Lines: [title, employment_type, salary?, location?, exp?, date, Save, Apply]
#   Location format: "Remote only • United States" or "Onsite or remote • NYC+1"
#   Salary format: "$150k – $170k"

WF_FIXTURE = [
    {
        "company": "Remesh",
        "title": "Software Engineer",
        "salary": "$150k – $170k",
        "location": "",
        "url": "https://wellfound.com/jobs/3938437-software-engineer",
    },
    {
        "company": "Sydecar",
        "title": "Staff Software Engineer",
        "salary": "$225k – $255k",
        "location": "Remote • New York City+1",
        "url": "https://wellfound.com/jobs/3756999-staff-software-engineer",
    },
    {
        "company": "Acme",
        "title": "Backend Engineer",
        "salary": "$120k – $160k",
        "location": "Onsite or remote • Chicago, IL",
        "url": "https://wellfound.com/jobs/1111111-backend-engineer",
    },
    {
        "company": "BadCo",
        "title": "",
        "salary": "",
        "location": "",
        "url": "",
    },
]


def test_transform_wellfound_jobs_happy_path():
    from pipeline.agents.discoverer import _transform_wellfound_jobs
    results = _transform_wellfound_jobs(WF_FIXTURE)
    assert len(results) == 3  # BadCo skipped
    assert results[0].title == "Software Engineer"
    assert results[0].company == "Remesh"
    assert results[0].source == "wellfound"


def test_transform_wellfound_jobs_salary_extracted():
    from pipeline.agents.discoverer import _transform_wellfound_jobs
    results = _transform_wellfound_jobs(WF_FIXTURE)
    assert results[0].compensation_low == 150000
    assert results[0].compensation_high == 170000


def test_transform_wellfound_jobs_remote_detected():
    from pipeline.agents.discoverer import _transform_wellfound_jobs
    from pipeline.state import WorkplaceType
    results = _transform_wellfound_jobs(WF_FIXTURE)
    assert results[1].workplace_type == WorkplaceType.REMOTE


def test_transform_wellfound_jobs_hybrid_detected():
    from pipeline.agents.discoverer import _transform_wellfound_jobs
    from pipeline.state import WorkplaceType
    results = _transform_wellfound_jobs(WF_FIXTURE)
    assert results[2].workplace_type == WorkplaceType.HYBRID


def test_transform_wellfound_jobs_location_after_bullet():
    from pipeline.agents.discoverer import _transform_wellfound_jobs
    results = _transform_wellfound_jobs(WF_FIXTURE)
    # "Remote • New York City+1" → location = "New York City+1"
    assert results[1].location == "New York City+1"
    # "Onsite or remote • Chicago, IL" → location = "Chicago, IL"
    assert results[2].location == "Chicago, IL"


def test_transform_wellfound_jobs_vague_location_blank():
    from pipeline.agents.discoverer import _transform_wellfound_jobs
    results = _transform_wellfound_jobs(WF_FIXTURE)
    # Remesh has no location → blank
    assert results[0].location == ""


def test_transform_wellfound_jobs_url_preserved():
    from pipeline.agents.discoverer import _transform_wellfound_jobs
    results = _transform_wellfound_jobs(WF_FIXTURE)
    assert results[0].source_url == "https://wellfound.com/jobs/3938437-software-engineer"


def test_transform_wellfound_jobs_skips_empty_title():
    from pipeline.agents.discoverer import _transform_wellfound_jobs
    results = _transform_wellfound_jobs(WF_FIXTURE)
    assert "BadCo" not in [r.company for r in results]


def test_transform_wellfound_jobs_empty_input():
    from pipeline.agents.discoverer import _transform_wellfound_jobs
    assert _transform_wellfound_jobs([]) == []


@pytest.mark.asyncio
async def test_scrape_wellfound_no_playwright_returns_empty(monkeypatch):
    """scrape_wellfound returns empty gracefully when Playwright is unavailable."""
    import pipeline.agents.discoverer as disc
    monkeypatch.setattr(disc, "async_playwright", None)
    state = empty_state()
    state["search_params"] = SearchParams(query="engineer", location="Chicago, IL")
    assert await disc.scrape_wellfound(state) == {"raw_results": []}


def test_transform_builtin_jobs_skips_empty_title():
    from pipeline.agents.discoverer import _transform_builtin_jobs
    results = _transform_builtin_jobs(BUILTIN_FIXTURE)
    assert "BadCo" not in [r.company for r in results]


def test_transform_builtin_jobs_empty_input():
    from pipeline.agents.discoverer import _transform_builtin_jobs
    assert _transform_builtin_jobs([]) == []


@pytest.mark.asyncio
async def test_scrape_builtin_no_playwright_returns_empty(monkeypatch):
    """scrape_builtin returns empty gracefully when Playwright is unavailable."""
    import pipeline.agents.discoverer as disc
    monkeypatch.setattr(disc, "async_playwright", None)
    state = empty_state()
    state["search_params"] = SearchParams(query="engineer", location="Chicago, IL")
    assert await disc.scrape_builtin(state) == {"raw_results": []}


# ── F1: --dry-run flag ────────────────────────────────────────────────────────

import sqlite3


def _apply_all_migrations(db_path: str) -> None:
    """Apply every migrations/*.sql file to a fresh SQLite DB (test helper)."""
    conn = sqlite3.connect(db_path)
    migrations_dir = Path(__file__).parent.parent / "migrations"
    for migration_file in sorted(migrations_dir.glob("*.sql")):
        try:
            conn.executescript(migration_file.read_text())
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


def _make_shortlist(n: int) -> list:
    from pipeline.state import JobListing
    return [
        JobListing(
            source="indeed",
            title=f"Role {i}",
            company=f"Company {i}",
            location="Chicago, IL",
            source_url=f"https://example.com/job/{i}",
            fingerprint=f"fp_dryrun_{i:02d}",
        )
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_dry_run_flag_skips_db_write(tmp_path, monkeypatch):
    """dry_run=True: persist_to_db writes zero rows to the jobs table."""
    from pipeline import database as db_module
    from pipeline.agents import discoverer as disc

    db_path = str(tmp_path / "dryrun.db")
    _apply_all_migrations(db_path)
    monkeypatch.setattr(db_module.settings, "app_db_path", db_path)

    state = empty_state()
    state["search_params"] = SearchParams(query="x", location="y")
    state["shortlist"] = _make_shortlist(2)
    state["dry_run"] = True  # type: ignore[typeddict-unknown-key]

    await disc.persist_to_db(state)

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    conn.close()
    assert count == 0


@pytest.mark.asyncio
async def test_dry_run_false_writes_db(tmp_path, monkeypatch):
    """dry_run=False: persist_to_db writes every approved listing."""
    from pipeline import database as db_module
    from pipeline.agents import discoverer as disc

    db_path = str(tmp_path / "dryrun_off.db")
    _apply_all_migrations(db_path)
    monkeypatch.setattr(db_module.settings, "app_db_path", db_path)

    state = empty_state()
    state["search_params"] = SearchParams(query="x", location="y")
    state["shortlist"] = _make_shortlist(2)
    state["dry_run"] = False  # type: ignore[typeddict-unknown-key]

    await disc.persist_to_db(state)

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    conn.close()
    assert count == 2


def test_dry_run_defaults_to_false():
    """empty_state() initializes dry_run to False."""
    state = empty_state()
    assert state["dry_run"] is False  # type: ignore[typeddict-unknown-key]


@pytest.mark.asyncio
async def test_dry_run_prints_summary(tmp_path, monkeypatch, capsys):
    """dry_run=True prints a summary line with the count and company::title pairs."""
    from pipeline import database as db_module
    from pipeline.agents import discoverer as disc

    db_path = str(tmp_path / "dryrun_summary.db")
    _apply_all_migrations(db_path)
    monkeypatch.setattr(db_module.settings, "app_db_path", db_path)

    state = empty_state()
    state["search_params"] = SearchParams(query="x", location="y")
    shortlist = _make_shortlist(3)
    state["shortlist"] = shortlist
    state["dry_run"] = True  # type: ignore[typeddict-unknown-key]

    await disc.persist_to_db(state)

    captured = capsys.readouterr().out
    assert "3" in captured
    assert any(f"{j.company}::{j.title}" in captured for j in shortlist)


# ── F2: company::role dual dedup ──────────────────────────────────────────────


def _mock_get_connection_with_rows(rows):
    """Build a mock get_connection() returning the given list of dict rows."""

    class FakeAsyncCursor:
        def __init__(self, items):
            self._iter = iter(items)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._iter)
            except StopIteration:
                raise StopAsyncIteration

    class FakeConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def execute(self, *a, **kw):
            return FakeAsyncCursor(rows)

    def factory():
        return FakeConn()

    return factory


@pytest.mark.asyncio
async def test_merge_suppresses_already_applied_company_role(monkeypatch):
    """Same company+title at status=APPLIED (different URL/location) → suppressed."""
    import pipeline.agents.discoverer as disc

    raw = [
        RawJobListing(
            source="dice",
            title="Senior Engineer",
            company="Acme Corp",
            location="Chicago, IL",
            source_url="https://dice.com/j/new",
        )
    ]
    raw_fp = _make_fingerprint("Acme Corp", "Senior Engineer", "Chicago, IL")
    rows = [
        {
            # different fingerprint (different location) but same company::title
            "fingerprint": "different_fp_111",
            "status": "applied",
            "company": "Acme Corp",
            "title": "Senior Engineer",
        }
    ]
    assert rows[0]["fingerprint"] != raw_fp
    monkeypatch.setattr(disc, "get_connection", _mock_get_connection_with_rows(rows))

    state = empty_state()
    state["raw_results"] = raw  # type: ignore[assignment]
    result = await disc.merge_results(state)
    assert result["shortlist"] == []


@pytest.mark.asyncio
async def test_merge_suppresses_skipped_company_role(monkeypatch):
    """Same company+title at status=SKIPPED (different fingerprint) → suppressed."""
    import pipeline.agents.discoverer as disc

    raw = [
        RawJobListing(
            source="linkedin",
            title="Backend Engineer",
            company="DataCo",
            location="New York, NY",
            source_url="https://linkedin.com/j/ignored",
        )
    ]
    rows = [
        {
            "fingerprint": "unrelated_fp",
            "status": "skipped",
            "company": "DataCo",
            "title": "Backend Engineer",
        }
    ]
    monkeypatch.setattr(disc, "get_connection", _mock_get_connection_with_rows(rows))

    state = empty_state()
    state["raw_results"] = raw  # type: ignore[assignment]
    result = await disc.merge_results(state)
    assert result["shortlist"] == []


@pytest.mark.asyncio
async def test_merge_does_not_suppress_different_title_same_company(monkeypatch):
    """Same company but different title → not suppressed."""
    import pipeline.agents.discoverer as disc

    raw = [
        RawJobListing(
            source="dice",
            title="Backend Engineer",
            company="Acme Corp",
            location="Chicago, IL",
            source_url="https://dice.com/j/be",
        )
    ]
    rows = [
        {
            "fingerprint": "some_fp",
            "status": "applied",
            "company": "Acme Corp",
            "title": "Senior Engineer",  # different title
        }
    ]
    monkeypatch.setattr(disc, "get_connection", _mock_get_connection_with_rows(rows))

    state = empty_state()
    state["raw_results"] = raw  # type: ignore[assignment]
    result = await disc.merge_results(state)
    assert len(result["shortlist"]) == 1
    assert result["shortlist"][0].title == "Backend Engineer"


@pytest.mark.asyncio
async def test_merge_tags_queued_company_role_with_previously_seen_note(monkeypatch):
    """Same company+title at status=QUEUED → kept but tagged with previously-seen note."""
    import pipeline.agents.discoverer as disc

    raw = [
        RawJobListing(
            source="linkedin",
            title="Platform Engineer",
            company="Beta Inc",
            location="Remote",
            source_url="https://linkedin.com/j/queued",
        )
    ]
    rows = [
        {
            "fingerprint": "queued_fp",
            "status": "queued",
            "company": "Beta Inc",
            "title": "Platform Engineer",
        }
    ]
    monkeypatch.setattr(disc, "get_connection", _mock_get_connection_with_rows(rows))

    state = empty_state()
    state["raw_results"] = raw  # type: ignore[assignment]
    result = await disc.merge_results(state)
    assert len(result["shortlist"]) == 1
    assert result["shortlist"][0].notes == "[previously seen — status: queued]"


@pytest.mark.asyncio
async def test_merge_tags_docs_ready_company_role_with_previously_seen_note(monkeypatch):
    """Same company+title at status=DOCS_READY → kept but tagged with note."""
    import pipeline.agents.discoverer as disc

    raw = [
        RawJobListing(
            source="dice",
            title="Rails Developer",
            company="Gamma LLC",
            location="Austin, TX",
            source_url="https://dice.com/j/docs",
        )
    ]
    rows = [
        {
            "fingerprint": "docs_ready_fp",
            "status": "docs_ready",
            "company": "Gamma LLC",
            "title": "Rails Developer",
        }
    ]
    monkeypatch.setattr(disc, "get_connection", _mock_get_connection_with_rows(rows))

    state = empty_state()
    state["raw_results"] = raw  # type: ignore[assignment]
    result = await disc.merge_results(state)
    assert len(result["shortlist"]) == 1
    assert result["shortlist"][0].notes == "[previously seen — status: docs_ready]"


# ── F3: title filter pure function ────────────────────────────────────────────


def test_title_filter_positive_match():
    from pipeline.agents.discoverer import _build_title_filter
    allow = _build_title_filter(["engineer", "developer"], [])
    assert allow("Senior Software Engineer") is True
    assert allow("Backend Developer") is True
    assert allow("Product Manager") is False


def test_title_filter_negative_exclusion():
    from pipeline.agents.discoverer import _build_title_filter
    allow = _build_title_filter([], ["manager", "director"])
    assert allow("Engineering Manager") is False
    assert allow("Director of Engineering") is False
    assert allow("Senior Engineer") is True


def test_title_filter_empty_positive_allows_all():
    from pipeline.agents.discoverer import _build_title_filter
    allow = _build_title_filter([], ["intern"])
    assert allow("Data Scientist") is True
    assert allow("Platform Engineer") is True
    assert allow("Software Engineering Intern") is False


def test_title_filter_case_insensitive():
    from pipeline.agents.discoverer import _build_title_filter
    allow = _build_title_filter(["engineer"], ["MANAGER"])
    assert allow("SENIOR ENGINEER") is True
    assert allow("engineering MANAGER") is False
    assert allow("Engineering Manager") is False


def test_title_filter_negative_takes_priority():
    from pipeline.agents.discoverer import _build_title_filter
    allow = _build_title_filter(["engineer"], ["staff"])
    assert allow("Staff Engineer") is False


def test_title_filter_both_empty_allows_all():
    from pipeline.agents.discoverer import _build_title_filter
    allow = _build_title_filter([], [])
    assert allow("Any Title") is True
    assert allow("") is True


@pytest.mark.asyncio
async def test_merge_results_filters_titles_before_dedup(monkeypatch):
    """Filtered-out titles are dropped before fingerprint/DB lookup."""
    import pipeline.agents.discoverer as disc
    from pipeline import config as config_module

    raw = [
        RawJobListing(
            source="dice",
            title="Engineering Manager",
            company="Acme",
            location="Chicago, IL",
            source_url="https://dice.com/j/1",
        ),
        RawJobListing(
            source="dice",
            title="Senior Engineer",
            company="Acme",
            location="Chicago, IL",
            source_url="https://dice.com/j/2",
        ),
    ]
    monkeypatch.setattr(
        config_module.settings, "title_filter_positive", "", raising=False
    )
    monkeypatch.setattr(
        config_module.settings, "title_filter_negative", "manager", raising=False
    )
    monkeypatch.setattr(disc, "get_connection", _mock_get_connection_with_rows([]))

    state = empty_state()
    state["raw_results"] = raw  # type: ignore[assignment]
    result = await disc.merge_results(state)
    titles = [j.title for j in result["shortlist"]]
    assert "Engineering Manager" not in titles
    assert "Senior Engineer" in titles


def test_cli_dry_run_sets_state_true(monkeypatch):
    """CLI --dry-run/--no-dry-run propagate into the initial PipelineState."""
    from typer.testing import CliRunner

    from pipeline import cli as cli_module

    captured_states: list[dict] = []

    class FakeSnap:
        values: dict = {"shortlist": []}

    class FakeGraph:
        async def ainvoke(self, initial, config=None):
            captured_states.append(dict(initial))
            from langgraph.errors import GraphInterrupt

            raise GraphInterrupt()

        async def aget_state(self, config):
            return FakeSnap()

    class FakeCompiledFactory:
        def compile(self, **kwargs):
            return FakeGraph()

    def fake_build_graph():
        return FakeCompiledFactory()

    class FakeCheckpointer:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    class FakeSaver:
        @classmethod
        def from_conn_string(cls, path):
            return FakeCheckpointer()

    import pipeline.agents.discoverer as disc_module
    monkeypatch.setattr(disc_module, "build_discoverer_graph", fake_build_graph)
    monkeypatch.setattr(
        "langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver", FakeSaver
    )

    runner = CliRunner()

    result = runner.invoke(
        cli_module.app, ["discover", "--dry-run", "--sources", "indeed"]
    )
    assert result.exit_code == 0, result.output
    assert captured_states, "graph.ainvoke was never called"
    assert captured_states[-1].get("dry_run") is True

    captured_states.clear()
    result = runner.invoke(
        cli_module.app, ["discover", "--no-dry-run", "--sources", "indeed"]
    )
    assert result.exit_code == 0, result.output
    assert captured_states, "graph.ainvoke was never called"
    assert captured_states[-1].get("dry_run") is False
