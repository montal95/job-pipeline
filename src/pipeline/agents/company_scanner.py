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

import re
from typing import Literal
from urllib.parse import urlparse

from pipeline.agents.discoverer import _parse_compensation
from pipeline.state import RawJobListing

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
