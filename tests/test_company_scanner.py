"""
Red-first tests for the company scanner pure functions.

detect_ats_api() — maps a public careers URL to the JSON API endpoint and
    returns (api_url, ats_marker) where ats_marker ∈ {"greenhouse","ashby","lever"}.
_parse_greenhouse_jobs() / _parse_ashby_jobs() / _parse_lever_jobs() —
    turn decoded JSON into list[RawJobListing] with a per-ATS `source` field.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.state import RawJobListing

FIXTURES = Path(__file__).parent / "fixtures"


def _load_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ── detect_ats_api ────────────────────────────────────────────────────────────


def test_detect_ats_api_greenhouse_url():
    from pipeline.agents.company_scanner import detect_ats_api
    api_url, marker = detect_ats_api("https://boards.greenhouse.io/acme")
    assert marker == "greenhouse"
    assert "acme" in api_url
    assert "greenhouse" in api_url


def test_detect_ats_api_ashby_url():
    from pipeline.agents.company_scanner import detect_ats_api
    api_url, marker = detect_ats_api("https://jobs.ashbyhq.com/acme")
    assert marker == "ashby"
    assert "acme" in api_url
    assert "ashby" in api_url.lower()


def test_detect_ats_api_lever_url():
    from pipeline.agents.company_scanner import detect_ats_api
    api_url, marker = detect_ats_api("https://jobs.lever.co/acme")
    assert marker == "lever"
    assert "acme" in api_url
    assert "lever" in api_url


def test_detect_ats_api_explicit_greenhouse_api_url():
    """Already-an-API URL passes through unchanged."""
    from pipeline.agents.company_scanner import detect_ats_api
    original = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    api_url, marker = detect_ats_api(original)
    assert marker == "greenhouse"
    assert api_url == original


def test_detect_ats_api_unknown_url_returns_none():
    from pipeline.agents.company_scanner import detect_ats_api
    assert detect_ats_api("https://acme.com/careers") is None


# ── Parser functions ──────────────────────────────────────────────────────────


def test_parse_greenhouse_jobs_basic():
    from pipeline.agents.company_scanner import _parse_greenhouse_jobs
    data = _load_json("greenhouse_jobs_api.json")
    results = _parse_greenhouse_jobs(data, company="Acme")
    assert len(results) == 3
    assert all(isinstance(r, RawJobListing) for r in results)
    assert all(r.source == "greenhouse" for r in results)
    first = results[0]
    assert first.title == "Senior Backend Engineer"
    assert first.company == "Acme"
    assert "greenhouse.io" in first.source_url
    assert "Remote" in first.location


def test_parse_ashby_jobs_basic():
    from pipeline.agents.company_scanner import _parse_ashby_jobs
    data = _load_json("ashby_jobs_api.json")
    results = _parse_ashby_jobs(data, company="Acme")
    assert len(results) == 2
    assert all(r.source == "ashby" for r in results)
    first = results[0]
    assert first.title == "Backend Software Engineer"
    assert first.company == "Acme"
    assert "ashbyhq.com" in first.source_url
    assert "Remote" in first.location


def test_parse_lever_jobs_basic_with_compensation():
    from pipeline.agents.company_scanner import _parse_lever_jobs
    data = _load_json("lever_jobs_api.json")
    results = _parse_lever_jobs(data, company="Acme")
    assert len(results) == 2
    assert all(r.source == "lever" for r in results)
    first = results[0]
    assert first.title == "Senior Backend Engineer"
    assert first.company == "Acme"
    assert "lever.co" in first.source_url
    # compensation parsed from descriptionPlain "$160,000 - $200,000"
    assert first.compensation_low == 160000
    assert first.compensation_high == 200000


def test_parsers_skip_entries_without_title():
    from pipeline.agents.company_scanner import (
        _parse_greenhouse_jobs,
        _parse_ashby_jobs,
        _parse_lever_jobs,
    )
    gh = {"jobs": [{"absolute_url": "https://x", "location": {"name": "Remote"}}]}
    ash = {"jobs": [{"jobUrl": "https://x", "location": "Remote"}]}
    lev = [{"hostedUrl": "https://x", "categories": {"location": "Remote"}}]
    assert _parse_greenhouse_jobs(gh, company="Acme") == []
    assert _parse_ashby_jobs(ash, company="Acme") == []
    assert _parse_lever_jobs(lev, company="Acme") == []


def test_parsers_handle_missing_location_field():
    from pipeline.agents.company_scanner import (
        _parse_greenhouse_jobs,
        _parse_ashby_jobs,
        _parse_lever_jobs,
    )
    gh = {"jobs": [{"title": "Eng", "absolute_url": "https://x"}]}
    ash = {"jobs": [{"title": "Eng", "jobUrl": "https://x"}]}
    lev = [{"text": "Eng", "hostedUrl": "https://x"}]

    gh_res = _parse_greenhouse_jobs(gh, company="Acme")
    ash_res = _parse_ashby_jobs(ash, company="Acme")
    lev_res = _parse_lever_jobs(lev, company="Acme")

    assert len(gh_res) == 1 and gh_res[0].location == ""
    assert len(ash_res) == 1 and ash_res[0].location == ""
    assert len(lev_res) == 1 and lev_res[0].location == ""


# ── scrape_target_companies node ──────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, json_data=None):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=None,  # type: ignore[arg-type]
                response=None,  # type: ignore[arg-type]
            )


class _FakeAsyncClient:
    """Drop-in for httpx.AsyncClient configured with a url→response map."""

    def __init__(self, responses: dict[str, _FakeResponse], fetched: list[str]):
        self._responses = responses
        self._fetched = fetched

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def get(self, url: str, *args, **kwargs):
        self._fetched.append(url)
        if url not in self._responses:
            import httpx
            raise httpx.RequestError(f"no mock for {url}")
        resp = self._responses[url]
        if resp.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(
                f"status {resp.status_code}",
                request=None,  # type: ignore[arg-type]
                response=None,  # type: ignore[arg-type]
            )
        return resp


@pytest.mark.asyncio
async def test_scrape_target_companies_returns_empty_when_config_missing(monkeypatch, tmp_path):
    from pipeline.agents import company_scanner

    missing = tmp_path / "does_not_exist.yml"
    monkeypatch.setattr(company_scanner, "COMPANIES_CONFIG_PATH", missing)

    result = await company_scanner.scrape_target_companies({})
    assert result == {"raw_results": []}


@pytest.mark.asyncio
async def test_scrape_target_companies_skips_disabled_companies(monkeypatch, tmp_path):
    from pipeline.agents import company_scanner

    cfg = tmp_path / "companies.yml"
    cfg.write_text(
        """
companies:
  - name: Enabled Co
    url: https://boards.greenhouse.io/enabled
    enabled: true
  - name: Disabled Co
    url: https://boards.greenhouse.io/disabled
    enabled: false
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(company_scanner, "COMPANIES_CONFIG_PATH", cfg)

    fetched: list[str] = []
    data = _load_json("greenhouse_jobs_api.json")
    responses = {
        "https://boards-api.greenhouse.io/v1/boards/enabled/jobs":
            _FakeResponse(json_data=data),
    }
    monkeypatch.setattr(
        company_scanner.httpx, "AsyncClient",
        lambda *a, **kw: _FakeAsyncClient(responses, fetched),
    )

    result = await company_scanner.scrape_target_companies({})
    assert all("disabled" not in u for u in fetched)
    assert any("enabled" in u for u in fetched)
    assert len(result["raw_results"]) == 3


@pytest.mark.asyncio
async def test_scrape_target_companies_continues_on_per_company_failure(monkeypatch, tmp_path):
    from pipeline.agents import company_scanner

    cfg = tmp_path / "companies.yml"
    cfg.write_text(
        """
companies:
  - name: Good Co
    url: https://boards.greenhouse.io/good
    enabled: true
  - name: Bad Co
    url: https://jobs.ashbyhq.com/bad
    enabled: true
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(company_scanner, "COMPANIES_CONFIG_PATH", cfg)

    fetched: list[str] = []
    gh_data = _load_json("greenhouse_jobs_api.json")
    responses = {
        "https://boards-api.greenhouse.io/v1/boards/good/jobs":
            _FakeResponse(json_data=gh_data),
        "https://api.ashbyhq.com/posting-api/job-board/bad":
            _FakeResponse(status_code=500),
    }
    monkeypatch.setattr(
        company_scanner.httpx, "AsyncClient",
        lambda *a, **kw: _FakeAsyncClient(responses, fetched),
    )

    result = await company_scanner.scrape_target_companies({})
    # Bad Co's 500 is swallowed; Good Co's 3 listings remain
    sources = {r.source for r in result["raw_results"]}
    assert sources == {"greenhouse"}
    assert len(result["raw_results"]) == 3


@pytest.mark.asyncio
async def test_scrape_target_companies_does_not_request_brotli(monkeypatch, tmp_path):
    """Ashby's API serves brotli when the client advertises 'br' in
    Accept-Encoding. httpx only auto-decompresses brotli when the optional
    'brotli' (or 'brotlicffi') package is installed — which we don't ship.
    The result was bytes that look like gzip leaking into resp.json() and
    crashing with a misleading utf-8 decode error.

    This test pins the watchlist scanner to headers that do NOT request
    brotli, forcing servers to fall back to gzip (which httpx handles
    natively via the stdlib)."""
    from pipeline.agents import company_scanner

    cfg = tmp_path / "companies.yml"
    cfg.write_text(
        """
companies:
  - name: Some Co
    url: https://boards.greenhouse.io/someco
    enabled: true
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(company_scanner, "COMPANIES_CONFIG_PATH", cfg)

    captured_kwargs: list[dict] = []

    def _capturing_client_factory(*args, **kwargs):
        captured_kwargs.append(kwargs)
        return _FakeAsyncClient(
            {
                "https://boards-api.greenhouse.io/v1/boards/someco/jobs":
                    _FakeResponse(json_data={"jobs": []}),
            },
            [],
        )

    monkeypatch.setattr(
        company_scanner.httpx, "AsyncClient", _capturing_client_factory
    )

    await company_scanner.scrape_target_companies({})

    assert captured_kwargs, "httpx.AsyncClient was never instantiated"
    headers = captured_kwargs[0].get("headers") or {}
    accept_enc = headers.get("Accept-Encoding", "")
    assert "br" not in accept_enc.lower().split(", "), (
        f"Accept-Encoding includes brotli ({accept_enc!r}); httpx cannot "
        "auto-decompress brotli without the optional 'brotli' package, so "
        "Ashby's response will arrive as raw bytes and json() will crash"
    )


@pytest.mark.asyncio
async def test_scrape_target_companies_uses_generous_timeout(monkeypatch, tmp_path):
    """Lever boards with 400+ postings take >15s to respond; the watchlist
    scanner must allow a longer timeout than the marketplace scrapers, or
    legitimate large boards get falsely classified as transient failures.

    Regression guard: pins the AsyncClient timeout at >= 30s. If a future
    refactor moves to per-request timeouts, update this test accordingly."""
    from pipeline.agents import company_scanner

    cfg = tmp_path / "companies.yml"
    cfg.write_text(
        """
companies:
  - name: Some Co
    url: https://boards.greenhouse.io/someco
    enabled: true
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(company_scanner, "COMPANIES_CONFIG_PATH", cfg)

    captured_kwargs: list[dict] = []

    def _capturing_client_factory(*args, **kwargs):
        captured_kwargs.append(kwargs)
        return _FakeAsyncClient(
            {
                "https://boards-api.greenhouse.io/v1/boards/someco/jobs":
                    _FakeResponse(json_data={"jobs": []}),
            },
            [],
        )

    monkeypatch.setattr(
        company_scanner.httpx, "AsyncClient", _capturing_client_factory
    )

    await company_scanner.scrape_target_companies({})

    assert captured_kwargs, "httpx.AsyncClient was never instantiated"
    timeout = captured_kwargs[0].get("timeout", 0)
    assert timeout >= 30, (
        f"watchlist timeout is {timeout}s; should be >=30s to accommodate "
        "large Lever boards"
    )


@pytest.mark.asyncio
async def test_scrape_target_companies_logs_http_status_on_4xx(monkeypatch, tmp_path, capsys):
    """A 404 response (typo'd slug) must surface as 'HTTP 404 — slug probably
    wrong', not as the misleading 'parse failed (Expecting value: line 1
    column 1)' that bubbles up from json() trying to decode 'Not Found'.

    Real httpx.get() never raises on non-2xx status — the response comes back
    intact and you opt into raise_for_status(). The scraper must check status
    explicitly before calling resp.json()."""
    from pipeline.agents import company_scanner

    cfg = tmp_path / "companies.yml"
    cfg.write_text(
        """
companies:
  - name: Typo Co
    url: https://boards.greenhouse.io/typo-slug
    enabled: true
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(company_scanner, "COMPANIES_CONFIG_PATH", cfg)

    # Fake that mirrors real httpx behavior: get() returns the response even
    # for 4xx/5xx; json() on a non-JSON body raises ValueError (as the real
    # stdlib decoder does on "Not Found").
    class _RealisticResponse:
        status_code = 404
        text = "Not Found"
        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    class _RealisticClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def get(self, url, *a, **kw):
            return _RealisticResponse()

    monkeypatch.setattr(
        company_scanner.httpx, "AsyncClient",
        lambda *a, **kw: _RealisticClient(),
    )

    result = await company_scanner.scrape_target_companies({})
    captured = capsys.readouterr()

    assert result == {"raw_results": []}
    assert "HTTP 404" in captured.out
    assert "parse failed" not in captured.out


@pytest.mark.asyncio
async def test_scrape_target_companies_merges_across_ats_types(monkeypatch, tmp_path):
    from pipeline.agents import company_scanner

    cfg = tmp_path / "companies.yml"
    cfg.write_text(
        """
companies:
  - name: GH Co
    url: https://boards.greenhouse.io/ghco
    enabled: true
  - name: Ashby Co
    url: https://jobs.ashbyhq.com/ashbyco
    enabled: true
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(company_scanner, "COMPANIES_CONFIG_PATH", cfg)

    fetched: list[str] = []
    responses = {
        "https://boards-api.greenhouse.io/v1/boards/ghco/jobs":
            _FakeResponse(json_data=_load_json("greenhouse_jobs_api.json")),
        "https://api.ashbyhq.com/posting-api/job-board/ashbyco":
            _FakeResponse(json_data=_load_json("ashby_jobs_api.json")),
    }
    monkeypatch.setattr(
        company_scanner.httpx, "AsyncClient",
        lambda *a, **kw: _FakeAsyncClient(responses, fetched),
    )

    result = await company_scanner.scrape_target_companies({})
    sources = {r.source for r in result["raw_results"]}
    assert sources == {"greenhouse", "ashby"}
    companies = {r.company for r in result["raw_results"]}
    assert "GH Co" in companies
    assert "Ashby Co" in companies


# ── Fan-out integration ────────────────────────────────────────────────────────


def test_fan_out_includes_target_companies_when_in_sources():
    """When `target_companies` is in SearchParams.sources, fan_out_sources emits
    a Send for the scrape_target_companies node alongside the marketplace
    scrapers — confirming the routing key is wired end-to-end."""
    from pipeline.agents.discoverer import SOURCE_NODE_MAP, fan_out_sources
    from pipeline.state import SearchParams, empty_state

    assert SOURCE_NODE_MAP["target_companies"] == "scrape_target_companies"

    state = empty_state()
    state["search_params"] = SearchParams(
        query="engineer",
        location="Chicago, IL",
        sources=["dice", "target_companies"],
    )

    sends = fan_out_sources(state)
    node_names = {s.node for s in sends}
    assert "scrape_target_companies" in node_names
    assert "scrape_dice" in node_names
