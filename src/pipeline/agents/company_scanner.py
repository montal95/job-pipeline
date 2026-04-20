"""
Company scanner — queries Greenhouse, Ashby, and Lever public JSON APIs for a
curated watchlist of target companies (configured in `config/companies.yml`).

Unlike the marketplace scrapers in discoverer.py, these APIs are public, return
structured JSON, and need no auth or Playwright. That makes them dramatically
cheaper and more reliable for companies we intentionally follow.

Routing:
  "target_companies" appears in SearchParams.sources as a single routing key,
  but each RawJobListing emitted gets a per-ATS `source` ("greenhouse", "ashby",
  or "lever") so downstream consumers see the origin system — matching the
  convention used for "indeed", "dice", "linkedin".
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
import yaml
from rich.console import Console

from pipeline.state import RawJobListing

# `discoverer` imports are deferred — discoverer imports this module as well
# (scrape_target_companies is registered in its SOURCE_NODE_MAP), so importing
# it at module scope here would form a cycle at load time.

console = Console()

# Config path — overridable in tests via monkeypatch.
COMPANIES_CONFIG_PATH: Path = (
    Path(__file__).parent.parent.parent.parent / "config" / "companies.yml"
)

# Lever boards with hundreds of postings (e.g. Lyra Health at ~400 jobs)
# routinely take 20-25s to respond. The marketplace scrapers' 15s ceiling
# is too tight for the watchlist scan; bump it so legitimate large boards
# don't get falsely classified as transient ReadTimeout failures.
WATCHLIST_HTTP_TIMEOUT_SECS: float = 30.0

AtsMarker = Literal["greenhouse", "ashby", "lever"]

_GREENHOUSE_BOARD = re.compile(
    r"^https?://boards\.greenhouse\.io/([^/?#]+)", re.IGNORECASE
)
_GREENHOUSE_API = re.compile(
    r"^https?://boards-api\.greenhouse\.io/", re.IGNORECASE
)
_ASHBY_BOARD = re.compile(
    r"^https?://jobs\.ashbyhq\.com/([^/?#]+)", re.IGNORECASE
)
_LEVER_BOARD = re.compile(
    r"^https?://jobs\.lever\.co/([^/?#]+)", re.IGNORECASE
)


def detect_ats_api(url: str) -> tuple[str, AtsMarker] | None:
    """
    Map a public careers URL to its JSON API endpoint + ATS marker.

    Returns None for unknown URLs so the caller can log-and-skip. Already-an-API
    URLs (e.g. boards-api.greenhouse.io/...) pass through unchanged.
    """
    if not url:
        return None

    if _GREENHOUSE_API.match(url):
        return (url, "greenhouse")

    m = _GREENHOUSE_BOARD.match(url)
    if m:
        slug = m.group(1)
        return (f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", "greenhouse")

    m = _ASHBY_BOARD.match(url)
    if m:
        slug = m.group(1)
        return (f"https://api.ashbyhq.com/posting-api/job-board/{slug}", "ashby")

    m = _LEVER_BOARD.match(url)
    if m:
        slug = m.group(1)
        return (f"https://api.lever.co/v0/postings/{slug}?mode=json", "lever")

    return None


def _parse_greenhouse_jobs(
    payload: dict, company: str
) -> list[RawJobListing]:
    results: list[RawJobListing] = []
    for job in payload.get("jobs", []):
        title = job.get("title")
        if not title:
            continue
        location_obj = job.get("location") or {}
        location = location_obj.get("name", "") if isinstance(location_obj, dict) else ""
        results.append(
            RawJobListing(
                source="greenhouse",
                title=title,
                company=company,
                location=location,
                source_url=job.get("absolute_url", "") or "",
            )
        )
    return results


def _parse_ashby_jobs(payload: dict, company: str) -> list[RawJobListing]:
    results: list[RawJobListing] = []
    for job in payload.get("jobs", []):
        title = job.get("title")
        if not title:
            continue
        location = job.get("location", "") or ""
        if isinstance(location, dict):
            location = location.get("name", "") or ""
        results.append(
            RawJobListing(
                source="ashby",
                title=title,
                company=company,
                location=location,
                source_url=job.get("jobUrl", "") or "",
            )
        )
    return results


def _parse_lever_jobs(
    payload: list[dict], company: str
) -> list[RawJobListing]:
    from pipeline.agents.discoverer import _parse_compensation

    results: list[RawJobListing] = []
    for job in payload:
        title = job.get("text")
        if not title:
            continue
        categories = job.get("categories", {}) or {}
        location = categories.get("location", "") or ""
        salary_text = " ".join(
            filter(
                None,
                [job.get("additional", ""), job.get("descriptionPlain", "")],
            )
        )
        comp_low, comp_high = _parse_compensation(salary_text)
        results.append(
            RawJobListing(
                source="lever",
                title=title,
                company=company,
                location=location,
                source_url=job.get("hostedUrl", "") or "",
                apply_url=job.get("applyUrl"),
                compensation_low=comp_low,
                compensation_high=comp_high,
            )
        )
    return results


# ── Graph node ────────────────────────────────────────────────────────────────


_PARSER_BY_MARKER = {
    "greenhouse": _parse_greenhouse_jobs,
    "ashby": _parse_ashby_jobs,
    "lever": _parse_lever_jobs,
}


async def scrape_target_companies(state: dict) -> dict:
    """
    Scan watchlist companies listed in config/companies.yml.

    Each enabled entry is mapped to its JSON API URL via detect_ats_api(),
    fetched concurrently, and parsed into RawJobListings with a per-ATS
    `source` field. Per-company failures are swallowed with a yellow warning
    — one bad endpoint never kills the whole scan.
    """
    if not COMPANIES_CONFIG_PATH.exists():
        console.print(
            f"⚠ Company watchlist not found at {COMPANIES_CONFIG_PATH}. "
            "Copy config/companies.example.yml → config/companies.yml to enable.",
            markup=False,
            style="yellow",
        )
        return {"raw_results": []}

    try:
        cfg = yaml.safe_load(COMPANIES_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        console.print(
            f"⚠ Failed to parse {COMPANIES_CONFIG_PATH}: {exc}",
            markup=False,
            style="yellow",
        )
        return {"raw_results": []}

    entries = [
        e for e in (cfg.get("companies") or [])
        if isinstance(e, dict) and e.get("enabled", True) and e.get("url")
    ]
    if not entries:
        return {"raw_results": []}

    # Resolve each entry to (name, api_url, marker). Skip entries we can't route.
    resolved: list[tuple[str, str, str]] = []
    for entry in entries:
        name = entry.get("name", entry["url"])
        mapping = detect_ats_api(entry["url"])
        if mapping is None:
            console.print(
                f"⚠ Unknown ATS for {name} ({entry['url']}) — skipping",
                markup=False,
                style="yellow",
            )
            continue
        api_url, marker = mapping
        resolved.append((name, api_url, marker))

    if not resolved:
        return {"raw_results": []}

    from pipeline.agents.discoverer import HEADERS

    results: list[RawJobListing] = []
    async with httpx.AsyncClient(
        headers=HEADERS, timeout=WATCHLIST_HTTP_TIMEOUT_SECS
    ) as client:
        fetches = [client.get(api_url) for (_, api_url, _) in resolved]
        responses = await asyncio.gather(*fetches, return_exceptions=True)

    for (name, api_url, marker), resp in zip(resolved, responses):
        if isinstance(resp, Exception):
            console.print(
                f"⚠ {name}: fetch failed ({resp.__class__.__name__}) — skipping",
                markup=False,
                style="yellow",
            )
            continue
        # httpx.get() doesn't raise on 4xx/5xx; check explicitly so a typo'd
        # slug surfaces as "HTTP 404" instead of a confusing JSON decode error.
        if resp.status_code >= 400:
            console.print(
                f"⚠ {name}: HTTP {resp.status_code} — slug probably wrong",
                markup=False,
                style="yellow",
            )
            continue
        try:
            payload = resp.json()
            parser = _PARSER_BY_MARKER[marker]
            parsed = parser(payload, company=name)
        except Exception as exc:
            console.print(
                f"⚠ {name}: parse failed ({exc}) — skipping",
                markup=False,
                style="yellow",
            )
            continue
        results.extend(parsed)
        console.print(
            f"[dim]{name} ({marker}): {len(parsed)} listings[/dim]"
        )

    return {"raw_results": results}
