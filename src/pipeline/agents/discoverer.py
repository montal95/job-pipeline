"""
Discoverer Agent — Phase 2 implementation.

Responsibility:
  Search Indeed, Dice (no-auth), LinkedIn, and ZipRecruiter (Playwright
  storage_state auth), deduplicate by fingerprint, cross-reference the DB
  to suppress already-seen listings, present a triage shortlist to the user,
  and persist approved listings to the jobs table.

LangGraph patterns exercised here:
  - Send API (via add_conditional_edges) for parallel fan-out across sources.
    Each scraper runs concurrently; raw_results are accumulated via the
    operator.add reducer on PipelineState.
  - interrupt() for the triage human-in-the-loop gate. The graph checkpoints
    at this boundary; the CLI resumes via Command(resume=decisions).

Playwright scrapers (LinkedIn, ZipRecruiter):
  - Run on Windows only (no Linux x86_64 wheel available).
  - Require a saved auth session at playwright/.auth/{platform}.json.
  - Generate the auth file once with: python scripts/save_auth.py --platform all
  - Detect missing auth file and stale sessions — warn and return [] gracefully.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus
from uuid import uuid4

import httpx
from bs4 import BeautifulSoup
from langgraph.graph import END, StateGraph
from langgraph.types import Send, interrupt
from rich.console import Console
from rich.table import Table
from rich import box

from pipeline.ats import ATS_PATTERNS, detect_ats
from pipeline.database import get_connection
from pipeline.state import (
    AtsType,
    JobListing,
    JobStatus,
    PipelineState,
    RawJobListing,
    WorkplaceType,
)

# async_playwright is optional — only available on Windows (sys_platform == 'win32').
# Imported at module level so tests can monkeypatch it without entering the try block.
try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None  # type: ignore[assignment]

console = Console()

# ── HTTP headers ───────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# ── Auth paths ─────────────────────────────────────────────────────────────────

AUTH_DIR = Path(__file__).parent.parent.parent.parent / "playwright" / ".auth"

LOGIN_PATTERNS: dict[str, list[str]] = {
    "linkedin": ["/login", "/checkpoint"],
    "ziprecruiter": ["/login", "/signin"],
}


# ── Pure helper functions ──────────────────────────────────────────────────────


def _make_fingerprint(company: str, title: str, location: str) -> str:
    """Stable 16-char hash of (company, title, location) for dedup."""
    key = f"{company.lower().strip()}|{title.lower().strip()}|{location.lower().strip()}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _parse_compensation(text: str) -> tuple[int | None, int | None]:
    text = text.replace(",", "")
    hourly = re.search(r"\$(\d+(?:\.\d+)?)\s*/\s*hr", text, re.IGNORECASE)
    if hourly:
        rate = float(hourly.group(1))
        return int(rate * 2080), int(rate * 2080)
    range_k = re.findall(r"\$(\d+(?:\.\d+)?)K", text, re.IGNORECASE)
    if len(range_k) >= 2:
        return int(float(range_k[0]) * 1000), int(float(range_k[1]) * 1000)
    if len(range_k) == 1:
        val = int(float(range_k[0]) * 1000)
        return val, val
    range_plain = re.findall(r"\$(\d{5,6})", text)
    if len(range_plain) >= 2:
        return int(range_plain[0]), int(range_plain[1])
    return None, None


def _extract_salary_from_description(description: str) -> tuple[int | None, int | None]:
    """
    Scan a job description for salary range text.
    LinkedIn often buries salary in the description body rather than exposing it
    as a structured field. Handles patterns like:
      - "Base salary range $202,300 - $238,000"
      - "Salary: $120,000 - $160,000 per year"
      - "Pay range: $95K - $115K"
      - "$140,000/year"
    Falls back to _parse_compensation for K-notation and hourly rates.
    """
    if not description:
        return None, None
    # Look for lines that smell like salary context
    salary_lines = [
        line for line in description.split("\n")
        if re.search(r"\$[\d,]+", line) and re.search(
            r"\b(salary|pay|compensation|range|base|annual|per year|hourly)\b",
            line, re.IGNORECASE
        )
    ]
    for line in salary_lines:
        low, high = _parse_compensation(line)
        if low:
            return low, high
    # Fallback: scan any line containing a dollar range pattern
    for line in description.split("\n"):
        if re.search(r"\$[\d,]+\s*[-–]\s*\$[\d,]+", line):
            low, high = _parse_compensation(line)
            if low:
                return low, high
    return None, None


def _get_auth_path(platform: str) -> Path:
    """Return the storage_state auth file path for a given platform."""
    return AUTH_DIR / f"{platform}.json"


def _is_login_redirect(url: str, platform: str) -> bool:
    """Return True if the current URL indicates a login redirect."""
    patterns = LOGIN_PATTERNS.get(platform, ["/login"])
    return any(p in url for p in patterns)


def _parse_linkedin_cards(html: str) -> list[RawJobListing]:
    """
    Parse LinkedIn job search results HTML into RawJobListing objects.
    Pure function — no browser dependency, fully unit testable.
    Skips cards missing a title or company element.
    """
    soup = BeautifulSoup(html, "html.parser")
    results: list[RawJobListing] = []

    # Extract description text from the detail panel (visible for selected card only).
    # Used as fallback salary source via _extract_salary_from_description.
    desc_el = soup.select_one(".jobs-description__content, .jobs-box__html-content")
    description_text = desc_el.get_text(separator="\n", strip=True) if desc_el else ""

    # Try authenticated DOM first (logged-in search results)
    auth_cards = soup.select("div.job-card-container")
    if auth_cards:
        for card in auth_cards:
            title_el = card.select_one("a.job-card-container__link span[aria-hidden='true']") \
                       or card.select_one("a.job-card-container__link")
            company_el = card.select_one(".artdeco-entity-lockup__subtitle span")
            location_el = card.select_one(".artdeco-entity-lockup__caption") \
                          or card.select_one("[class*='metadata'] li")
            salary_el = card.select_one("[class*='salary']")
            job_id = card.get("data-job-id", "")
            if not (title_el and company_el):
                continue
            title = title_el.get_text(strip=True)
            company = company_el.get_text(strip=True)
            location = location_el.get_text(strip=True) if location_el else ""
            source_url = f"https://www.linkedin.com/jobs/view/{job_id}" if job_id else ""
            comp_low, comp_high = _parse_compensation(
                salary_el.get_text(strip=True) if salary_el else ""
            )
            # Fallback 1: scan description text for buried salary (selected card only)
            if not comp_low and card.get("aria-current") == "page" and description_text:
                comp_low, comp_high = _extract_salary_from_description(description_text)
            # Fallback 2: salary embedded in title e.g. "Backend Engineer ($150k - $180k)"
            if not comp_low:
                comp_low, comp_high = _extract_salary_from_description(title)
            workplace = WorkplaceType.REMOTE if "remote" in location.lower() else None
            results.append(RawJobListing(
                source="linkedin",
                title=title,
                company=company,
                location=location,
                source_url=source_url,
                compensation_low=comp_low,
                compensation_high=comp_high,
                workplace_type=workplace,
            ))
        return results

    # Fallback: public (unauthenticated) DOM
    for card in soup.select("div.job-search-card"):
        title_el = card.select_one("h3.base-search-card__title")
        company_el = card.select_one("h4.base-search-card__subtitle")
        location_el = card.select_one("span.job-search-card__location")
        link_el = card.select_one("a.base-card__full-link")
        salary_el = card.select_one("span.job-search-card__salary-info")
        if not (title_el and company_el):
            continue
        title = title_el.get_text(strip=True)
        company = company_el.get_text(strip=True)
        location = location_el.get_text(strip=True) if location_el else ""
        href = link_el.get("href", "") if link_el else ""
        source_url = f"https://www.linkedin.com{href}" if href.startswith("/") else href
        comp_low, comp_high = _parse_compensation(
            salary_el.get_text(strip=True) if salary_el else ""
        )
        workplace = WorkplaceType.REMOTE if "remote" in location.lower() else None
        results.append(RawJobListing(
            source="linkedin",
            title=title,
            company=company,
            location=location,
            source_url=source_url,
            compensation_low=comp_low,
            compensation_high=comp_high,
            workplace_type=workplace,
        ))
    return results


def _parse_ziprecruiter_cards(html: str) -> list[RawJobListing]:
    """
    DEPRECATED: Previously used BS4 HTML parsing. Retained as a no-op stub
    so import references don't break. Actual parsing is done via Playwright
    JS extraction — see _transform_ziprecruiter_jobs().
    """
    return []


def _transform_ziprecruiter_jobs(
    jobs_data: list[dict], default_location: str = ""
) -> list[RawJobListing]:
    """
    Transform raw JS-extracted ZipRecruiter card data into RawJobListing objects.
    Pure function — no browser dependency, fully unit testable.

    Each dict in jobs_data has the shape returned by the Playwright page.evaluate():
      {
        'company': str,
        'title': str,
        'location': str,   # e.g. "Chicago, IL · Remote"
        'salary': str,     # e.g. "$105.60K - $144.70K/yr" or ""
        'url': str,
      }
    """
    results: list[RawJobListing] = []
    for job in jobs_data:
        company = job.get("company", "") or "Unknown"
        title = job.get("title", "")
        if not title or not company or company == "Unknown" and not title:
            continue
        location_raw = job.get("location", "") or default_location
        location_parts = [p.strip() for p in location_raw.split("·")]
        location = location_parts[0] if location_parts else location_raw
        workplace_hint = location_parts[1].lower() if len(location_parts) > 1 else ""
        salary_str = job.get("salary", "")
        url = job.get("url", "")
        comp_low, comp_high = _parse_compensation(salary_str)
        if not comp_low:
            comp_low, comp_high = _extract_salary_from_description(salary_str + "\n" + title)
        workplace = (
            WorkplaceType.REMOTE if "remote" in workplace_hint else
            WorkplaceType.HYBRID if "hybrid" in workplace_hint else
            WorkplaceType.ONSITE if "on-site" in workplace_hint or "onsite" in workplace_hint else None
        )
        results.append(RawJobListing(
            source="ziprecruiter",
            title=title,
            company=company,
            location=location,
            source_url=url,
            compensation_low=comp_low,
            compensation_high=comp_high,
            workplace_type=workplace,
        ))
    return results


# ── Indeed scraper (httpx + BS4, no auth) ─────────────────────────────────────


async def scrape_indeed(state: PipelineState) -> dict:
    """
    PARKED — Playwright fingerprint detected at login form level (March 2026).
    grep: INDEED_PARKED

    Indeed detects Playwright on the login form itself before any CAPTCHA can
    be solved — storage_state auth cannot be saved. Block is browser-fingerprint
    based, not IP based (unlike Wellfound).

    Full implementation preserved in commented code below. Card selectors and
    BS4 parsing logic were confirmed working against Indeed's HTML structure.

    Potential workarounds to research (same as Wellfound):
      1. playwright-extra + puppeteer-extra-plugin-stealth (patches fingerprint)
      2. Residential proxy with real browser headers
      3. Indeed Publisher API (requires application approval)
    """
    console.print("[dim]Indeed: parked (Playwright fingerprint detected) — skipping[/dim]")
    return {"raw_results": []}


# # ── scrape_indeed full implementation (INDEED_PARKED) ────────────────────────
# # Uncomment and re-wire into SOURCE_NODE_MAP + build_discoverer_graph when
# # a workaround for Indeed's Playwright fingerprint detection is found.
# #
# # Card selectors confirmed against Indeed's HTML structure:
# #   li.css-5lfssm, div.job_seen_beacon, div.tapItem
# #   title:    h2.jobTitle span[title] or h2.jobTitle a span
# #   company:  span.companyName or [data-testid='company-name']
# #   location: div.companyLocation or [data-testid='text-location']
# #   link:     a[id^='job_'], a.jcs-JobTitle, h2.jobTitle a
# #   salary:   div.metadata.salary-snippet-container
# #
# async def _scrape_indeed_impl(state: PipelineState) -> dict:
#     if async_playwright is None:
#         return {"raw_results": []}
#     params = state["search_params"]
#     query_str = params.query + (" remote" if params.remote else "")
#     url = (
#         f"https://www.indeed.com/jobs"
#         f"?q={quote_plus(query_str)}"
#         f"&l={quote_plus(params.location)}"
#         f"&sort=date&limit={params.max_results_per_source}"
#     )
#     auth_path = _get_auth_path("indeed")
#     results: list[RawJobListing] = []
#     try:
#         import asyncio as _asyncio
#         async with async_playwright() as p:
#             browser = await p.chromium.launch(headless=False, channel="chrome")
#             context = (
#                 await browser.new_context(storage_state=str(auth_path))
#                 if auth_path.exists()
#                 else await browser.new_context()
#             )
#             page = await context.new_page()
#             await page.goto(url, wait_until="domcontentloaded")
#             await _asyncio.sleep(3)
#             for sel in ("div.job_seen_beacon", "div.tapItem", "li.css-5lfssm"):
#                 try:
#                     await page.wait_for_selector(sel, timeout=5000)
#                     break
#                 except Exception:
#                     continue
#             html = await page.content()
#             await browser.close()
#         soup = BeautifulSoup(html, "html.parser")
#         for card in soup.select("li.css-5lfssm, div.job_seen_beacon, div.tapItem")[:params.max_results_per_source]:
#             title_el = card.select_one("h2.jobTitle span[title], h2.jobTitle a span")
#             company_el = card.select_one("span.companyName, [data-testid='company-name']")
#             location_el = card.select_one("div.companyLocation, [data-testid='text-location']")
#             link_el = card.select_one("a[id^='job_'], a.jcs-JobTitle, h2.jobTitle a")
#             salary_el = card.select_one("div.metadata.salary-snippet-container")
#             if not (title_el and company_el):
#                 continue
#             title = title_el.get("title") or title_el.get_text(strip=True)
#             company = company_el.get_text(strip=True)
#             location = location_el.get_text(strip=True) if location_el else params.location
#             href = link_el.get("href", "") if link_el else ""
#             source_url = f"https://www.indeed.com{href}" if href.startswith("/") else href
#             comp_low, comp_high = _parse_compensation(
#                 salary_el.get_text(strip=True) if salary_el else ""
#             )
#             results.append(RawJobListing(
#                 source="indeed", title=title, company=company, location=location,
#                 source_url=source_url or url, compensation_low=comp_low,
#                 compensation_high=comp_high,
#                 workplace_type=WorkplaceType.REMOTE if "remote" in location.lower() else None,
#             ))
#     except Exception as exc:
#         console.print(f"[yellow]⚠ Indeed scrape error: {exc}[/yellow]")
#     console.print(f"[dim]Indeed: {len(results)} listings[/dim]")
#     return {"raw_results": results}


# ── Dice scraper (JSON API, no auth) ──────────────────────────────────────────


async def scrape_dice(state: PipelineState) -> dict:
    """
    Scrape Dice via Playwright network interception.
    The Dice search API (dhigroupinc.com) is called server-side by Next.js, so
    the x-api-key is never sent to the browser and httpx requests are 403'd.
    Instead: load the Dice search page in a real browser, intercept the API
    response that fires in the background, and parse the JSON we already know.
    """
    if async_playwright is None:
        console.print("[yellow]⚠ Playwright not available — skipping Dice[/yellow]")
        return {"raw_results": []}

    params = state["search_params"]
    search_url = (
        f"https://www.dice.com/jobs"
        f"?q={quote_plus(params.query)}"
        f"&location={quote_plus(params.location)}"
        f"&countryCode=US&radius=30&radiusUnit=mi"
    )
    results: list[RawJobListing] = []
    jobs_data: list[dict] = []
    try:
        import asyncio as _asyncio
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, channel="chrome")
            page = await browser.new_page()
            await page.goto(search_url, wait_until="networkidle")
            await _asyncio.sleep(5)
            # Wait for job title links to render
            try:
                await page.wait_for_selector("[data-testid='job-search-job-detail-link']", timeout=10000)
            except Exception:
                console.print("[yellow]⚠ Dice: job cards did not appear[/yellow]")
                await browser.close()
                return {"raw_results": []}
            # Extract all job data via JS — walk up from title link to card container
            jobs_data = await page.evaluate("""() => {
                const links = document.querySelectorAll('[data-testid="job-search-job-detail-link"]');
                return Array.from(links).map(link => {
                    // Walk up the DOM to find the card container — try progressively higher parents
                    let card = link.parentElement;
                    for (let i = 0; i < 8; i++) {
                        if (!card) break;
                        const text = card.innerText || '';
                        const lines = text.split('\\n').map(s => s.trim()).filter(Boolean);
                        if (lines.length >= 3) break;
                        card = card.parentElement;
                    }
                    // The link lives in div[role="main"] (Details section).
                    // One level above is the full card with company name.
                    const detailsDiv = link.closest('[role="main"]');
                    const fullCard = detailsDiv ? detailsDiv.parentElement : card;
                    const lines = (fullCard ? fullCard.innerText : '').split('\\n').map(s => s.trim()).filter(Boolean);
                    // Structure: [company, cta_badge, title, location, '•', date, description, type, salary?]
                    return {
                        title: link.getAttribute('aria-label') || link.innerText.trim(),
                        url: link.href,
                        company: lines[0] || '',
                        location: lines[3] || '',
                        salary: lines.find(l => l.includes('$')) || '',
                        description: lines[6] || '',
                    };
                });
            }""")
            await browser.close()

        for job in jobs_data[:params.max_results_per_source]:
            title = job.get("title", "")
            job_url = job.get("url", "")
            company = job.get("company") or "Unknown"
            location = job.get("location") or params.location
            salary_str = job.get("salary", "")
            description = job.get("description", "")
            comp_low, comp_high = _parse_compensation(salary_str)
            # Fallback 1: scan description for buried salary e.g. "Salary: $170,000 - $210,000"
            if not comp_low:
                comp_low, comp_high = _extract_salary_from_description(salary_str + "\n" + description)
            # Fallback 2: salary embedded in title e.g. "Staff Engineer ($150k - $180k)"
            if not comp_low:
                comp_low, comp_high = _extract_salary_from_description(title)
            ws = location.lower()
            workplace = (
                WorkplaceType.REMOTE if "remote" in ws else
                WorkplaceType.HYBRID if "hybrid" in ws else
                WorkplaceType.ONSITE if "onsite" in ws else None
            )
            results.append(RawJobListing(
                source="dice", title=title, company=company, location=location,
                source_url=job_url or search_url,
                compensation_low=comp_low, compensation_high=comp_high,
                workplace_type=workplace,
            ))
    except Exception as exc:
        console.print(f"[yellow]⚠ Dice scrape error: {exc}[/yellow]")
    console.print(f"[dim]Dice: {len(results)} listings[/dim]")
    return {"raw_results": results}


# ── LinkedIn scraper (Playwright + storage_state) ─────────────────────────────


async def scrape_linkedin(state: PipelineState) -> dict:
    """
    Scrape LinkedIn job search via Playwright with a saved auth session.
    Windows only. Requires playwright/.auth/linkedin.json from save_auth.py.
    """
    auth_path = _get_auth_path("linkedin")
    if not auth_path.exists():
        console.print(
            "[yellow]⚠ LinkedIn auth file not found. "
            "Run: python scripts/save_auth.py --platform linkedin[/yellow]"
        )
        return {"raw_results": []}

    params = state["search_params"]
    search_url = (
        "https://www.linkedin.com/jobs/search/"
        f"?keywords={quote_plus(params.query)}"
        f"&location={quote_plus(params.location)}"
        "&sortBy=DD"
        + ("&f_WT=2" if params.remote else "")
    )
    results: list[RawJobListing] = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, channel="chrome")
            context = await browser.new_context(storage_state=str(auth_path))
            page = await context.new_page()
            await page.goto(search_url, wait_until="domcontentloaded")
            # Give the page extra time to settle after initial load
            import asyncio as _asyncio
            await _asyncio.sleep(3)
            if _is_login_redirect(page.url, "linkedin"):
                console.print(
                    "[yellow]⚠ LinkedIn session expired. "
                    "Run: python scripts/save_auth.py --platform linkedin[/yellow]"
                )
                await browser.close()
                return {"raw_results": []}
            # Debug: log current URL and a sample of the page so we can see what loaded
            console.print(f"[dim]LinkedIn page URL: {page.url}[/dim]")
            # Try authenticated selectors first, fall back to public ones
            AUTH_SELECTORS = (
                "li.jobs-search-results__list-item",
                "div.job-card-container",
                "div.scaffold-layout__list-container",
            )
            PUBLIC_SELECTORS = (
                "div.job-search-card",
                "ul.jobs-search__results-list",
            )
            found = False
            for sel in (*AUTH_SELECTORS, *PUBLIC_SELECTORS):
                try:
                    await page.wait_for_selector(sel, timeout=5000)
                    console.print(f"[dim]LinkedIn selector matched: {sel}[/dim]")
                    found = True
                    break
                except Exception:
                    continue
            if not found:
                # Dump a snippet of the page HTML for debugging
                snippet = (await page.content())[:2000]
                console.print(f"[yellow]⚠ LinkedIn: no known selector matched. Page snippet:\n{snippet}[/yellow]")
                await browser.close()
                return {"raw_results": []}
            html = await page.content()
            await browser.close()
        results = _parse_linkedin_cards(html)
    except Exception as exc:
        console.print(f"[yellow]⚠ LinkedIn scrape error: {exc}[/yellow]")
    console.print(f"[dim]LinkedIn: {len(results)} listings[/dim]")
    return {"raw_results": results}


# ── ZipRecruiter scraper (Playwright + storage_state) ────────────────────────


async def scrape_ziprecruiter(state: PipelineState) -> dict:
    """
    Scrape ZipRecruiter via Playwright (headless=False, no auth required).
    Public job search works without login. Bot detection blocks authenticated
    sessions but the public search page renders job cards freely.

    Card structure (confirmed via DevTools inspection):
      div[class*="job_result"] contains:
      lines: [company, title, location (city · workplace_type), salary?, ...]
    """
    if async_playwright is None:
        console.print("[yellow]⚠ Playwright not available — skipping ZipRecruiter[/yellow]")
        return {"raw_results": []}

    params = state["search_params"]
    search_url = (
        "https://www.ziprecruiter.com/jobs-search"
        f"?search={quote_plus(params.query)}"
        f"&location={quote_plus(params.location)}"
    )
    results: list[RawJobListing] = []
    jobs_data: list[dict] = []
    try:
        import asyncio as _asyncio
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, channel="chrome")
            page = await browser.new_page()
            await page.goto(search_url, wait_until="domcontentloaded")
            await _asyncio.sleep(3)
            try:
                await page.wait_for_selector("div[class*='job_result']", timeout=10000)
            except Exception:
                console.print("[yellow]⚠ ZipRecruiter: job cards did not appear[/yellow]")
                await browser.close()
                return {"raw_results": []}
            jobs_data = await page.evaluate("""() => {
                const cards = document.querySelectorAll('div[class*="job_result"]');
                return Array.from(cards).map(card => {
                    const lines = card.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
                    const link = card.querySelector('a[class*="job_link"], h2 a, a[class*="title"]')
                                 || card.querySelector('a');
                    return {
                        company: lines[0] || '',
                        title: lines[1] || '',
                        location: lines[2] || '',
                        salary: lines.find(l => l.includes('$')) || '',
                        url: link ? link.href : '',
                    };
                });
            }""")
            await browser.close()

        results = _transform_ziprecruiter_jobs(
            jobs_data[:params.max_results_per_source], params.location
        )
    except Exception as exc:
        console.print(f"[yellow]⚠ ZipRecruiter scrape error: {exc}[/yellow]")
    console.print(f"[dim]ZipRecruiter: {len(results)} listings[/dim]")
    return {"raw_results": results}


# ── Graph nodes ────────────────────────────────────────────────────────────────



# ── Built In scraper (Playwright, no auth) ────────────────────────────────────


def _transform_builtin_jobs(jobs_data: list[dict]) -> list[RawJobListing]:
    """
    Transform raw JS-extracted Built In card data into RawJobListing objects.
    Pure function — no browser dependency, fully unit testable.

    Each dict has the shape returned by the Playwright page.evaluate():
      { company, title, workplace, location, salary, url }
    Salary format: "165K-195K Annually" or None.
    """
    results: list[RawJobListing] = []
    for job in jobs_data:
        company = job.get("company", "") or "Unknown"
        title = job.get("title", "")
        if not title:
            continue
        workplace_raw = (job.get("workplace") or "").lower()
        location_raw = job.get("location") or ""
        # Keep specific city/state (e.g. "Chicago, IL, USA"), normalize vague country entries to blank
        vague = {"usa", "us", "united states", "2 locations", ""}
        location = "" if location_raw.strip().lower() in vague else location_raw
        salary_str = job.get("salary") or ""
        # Normalize Built In salary format — no $ signs, e.g. "165K-195K Annually"
        # Convert to "$165K-$195K/yr" which _parse_compensation handles natively
        if salary_str and not salary_str.startswith("$"):
            salary_str = salary_str.replace(" Annually", "/yr").replace(" Hourly", "/hr")
            salary_str = re.sub(r"(\d+(?:\.\d+)?K)", r"$\1", salary_str)
        url = job.get("url") or ""
        source_url = f"https://builtin.com{url}" if url.startswith("/") else url
        comp_low, comp_high = _parse_compensation(salary_str)
        workplace = (
            WorkplaceType.REMOTE if "remote" in workplace_raw else
            WorkplaceType.HYBRID if "hybrid" in workplace_raw else
            WorkplaceType.ONSITE if "in-office" in workplace_raw or "onsite" in workplace_raw else None
        )
        results.append(RawJobListing(
            source="builtin",
            title=title,
            company=company,
            location=location,
            source_url=source_url,
            compensation_low=comp_low,
            compensation_high=comp_high,
            workplace_type=workplace,
        ))
    return results


async def scrape_builtin(state: PipelineState) -> dict:
    """
    Scrape Built In Chicago via Playwright (headless=False, no auth).
    Card structure confirmed via Chrome DevTools inspection (March 2026):
      [data-id="job-card"] contains lines: [company, title, date, workplace, location, salary?, level]
      Job URL: card.querySelectorAll('a')[2].pathname → /job/{slug}/{id}
    """
    if async_playwright is None:
        console.print("[yellow]⚠ Playwright not available — skipping Built In[/yellow]")
        return {"raw_results": []}

    params = state["search_params"]
    search_url = (
        f"https://builtin.com/jobs/chicago"
        f"?search={quote_plus(params.query)}"
    )
    results: list[RawJobListing] = []
    jobs_data: list[dict] = []
    try:
        import asyncio as _asyncio
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, channel="chrome")
            page = await browser.new_page()
            await page.goto(search_url, wait_until="domcontentloaded")
            await _asyncio.sleep(3)
            try:
                await page.wait_for_selector("[data-id='job-card']", timeout=10000)
            except Exception:
                console.print("[yellow]⚠ Built In: job cards did not appear[/yellow]")
                await browser.close()
                return {"raw_results": []}
            jobs_data = await page.evaluate("""() => {
                const cards = document.querySelectorAll('[data-id="job-card"]');
                return Array.from(cards).map(card => {
                    const lines = card.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
                    const links = card.querySelectorAll('a');
                    const jobLink = Array.from(links).find(a => a.pathname.startsWith('/job/'));
                    const salaryLine = lines.find(l => l.match(/\\d+K.*Annually|\\d+K.*Hourly/i));
                    // Structure: [company, title, date, workplace, location, (salary?), level]
                    return {
                        company: lines[0] || '',
                        title: lines[1] || '',
                        workplace: lines[3] || '',
                        location: lines[4] || '',
                        salary: salaryLine || '',
                        url: jobLink ? jobLink.pathname : '',
                    };
                });
            }""")
            await browser.close()
        results = _transform_builtin_jobs(jobs_data[:params.max_results_per_source])
    except Exception as exc:
        console.print(f"[yellow]⚠ Built In scrape error: {exc}[/yellow]")
    console.print(f"[dim]Built In: {len(results)} listings[/dim]")
    return {"raw_results": results}



# ── Wellfound scraper (Playwright, no auth) ───────────────────────────────────


def _transform_wellfound_jobs(jobs_data: list[dict]) -> list[RawJobListing]:
    """
    Transform raw JS-extracted Wellfound job data into RawJobListing objects.
    Pure function — no browser dependency, fully unit testable.

    Each dict has the shape returned by the Playwright page.evaluate():
      { company, title, employment_type, salary, location, url }
    Salary format: "$150k – $170k" or "" if not listed.
    Location format: "Remote only • United States" or "Onsite or remote • NYC+2"
    """
    results: list[RawJobListing] = []
    for job in jobs_data:
        company = job.get("company", "") or "Unknown"
        title = job.get("title", "")
        if not title:
            continue
        salary_str = job.get("salary", "") or ""
        location_raw = job.get("location", "") or ""
        url = job.get("url", "") or ""
        # Parse location and workplace from "Remote only • United States" pattern
        if "•" in location_raw:
            parts = [p.strip() for p in location_raw.split("•")]
            workplace_hint = parts[0].lower()
            location = parts[1] if len(parts) > 1 else ""
        else:
            workplace_hint = location_raw.lower()
            location = ""
        # Normalize vague country entries
        vague = {"united states", "usa", "us", ""}
        if location.lower() in vague:
            location = ""
        # Normalize salary: "$150k – $170k" → parseable by _parse_compensation
        salary_str = salary_str.replace("k", "K").replace(" – ", "-").replace("–", "-")
        comp_low, comp_high = _parse_compensation(salary_str)
        workplace = (
            WorkplaceType.HYBRID if "hybrid" in workplace_hint or "onsite or remote" in workplace_hint else
            WorkplaceType.REMOTE if "remote" in workplace_hint else
            WorkplaceType.ONSITE if "onsite" in workplace_hint else None
        )
        results.append(RawJobListing(
            source="wellfound",
            title=title,
            company=company,
            location=location,
            source_url=url,
            compensation_low=comp_low,
            compensation_high=comp_high,
            workplace_type=workplace,
        ))
    return results


async def scrape_wellfound(state: PipelineState) -> dict:
    """
    PARKED — IP-based bot detection blocks Playwright (March 2026).
    grep: WELLFOUND_PARKED

    Wellfound returns "Access is temporarily restricted" citing automated
    activity from the IP. Block is network-level, not session/cookie level —
    persistent browser profiles do not help.

    Card structure was fully confirmed via Chrome DevTools inspection and is
    preserved in the commented implementation below. The _transform_wellfound_jobs()
    function and tests are also preserved so the plumbing is ready when a
    workaround is found.

    Potential workarounds to research:
      1. Residential proxy rotation
      2. playwright-extra + puppeteer-extra-plugin-stealth
      3. Official Wellfound API (requires partnership application)
    """
    console.print("[dim]Wellfound: parked (IP-based bot detection) — skipping[/dim]")
    return {"raw_results": []}


# # ── scrape_wellfound full implementation (WELLFOUND_PARKED) ──────────────────
# # Uncomment and re-wire into SOURCE_NODE_MAP + build_discoverer_graph when
# # a workaround for Wellfound's IP-based bot detection is found.
# #
# # Confirmed card structure (Chrome DevTools, March 2026):
# #   Company cards: .mb-6.w-full.rounded.border.border-gray-400.bg-white
# #   Job listings within card: .min-h-[50px] divs (one per role)
# #   Lines: [title, employment_type, salary?, location?, exp?, date, Save, Apply]
# #   Location: "Remote only • United States" or "Onsite or remote • City+N"
# #   Apply link: a[href*="/jobs/"] → full URL e.g. wellfound.com/jobs/3938437-slug
# #   Salary: "$150k – $170k" (starts with $, dash-separated)
# #
# async def _scrape_wellfound_impl(state: PipelineState) -> dict:
#     if async_playwright is None:
#         return {"raw_results": []}
#     params = state["search_params"]
#     search_url = (
#         f"https://wellfound.com/role/r/software-engineer"
#         f"?keywords={quote_plus(params.query)}"
#     )
#     results: list[RawJobListing] = []
#     jobs_data: list[dict] = []
#     try:
#         import asyncio as _asyncio
#         from pathlib import Path as _Path
#         user_data_dir = str(
#             _Path(__file__).parent.parent.parent / "playwright" / ".profiles" / "wellfound"
#         )
#         _Path(user_data_dir).mkdir(parents=True, exist_ok=True)
#         async with async_playwright() as p:
#             context = await p.chromium.launch_persistent_context(
#                 user_data_dir, headless=False, channel="chrome",
#             )
#             page = context.pages[0] if context.pages else await context.new_page()
#             await page.goto(search_url, wait_until="networkidle")
#             await _asyncio.sleep(6)
#             await page.wait_for_function("() => document.body !== null", timeout=10000)
#             try:
#                 await page.wait_for_selector(
#                     ".mb-6.w-full.rounded.border.border-gray-400.bg-white",
#                     timeout=15000,
#                 )
#             except Exception:
#                 page_text = await page.evaluate(
#                     "() => document.body?.innerText?.slice(0, 400) || 'no body'"
#                 )
#                 console.print(
#                     f"[yellow]⚠ Wellfound: cards not found. Page: {page_text}[/yellow]"
#                 )
#                 await context.close()
#                 return {"raw_results": []}
#             jobs_data = await page.evaluate("""() => {
#                 const cards = document.querySelectorAll(
#                     '.mb-6.w-full.rounded.border.border-gray-400.bg-white'
#                 );
#                 const out = [];
#                 cards.forEach(card => {
#                     const company = card.innerText.split('\\n')[0].trim();
#                     card.querySelectorAll('.min-h-\\\\[50px\\\\]').forEach(jd => {
#                         const lines = jd.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
#                         const link = jd.querySelector('a[href*="/jobs/"]');
#                         out.push({
#                             company,
#                             title: lines[0] || '',
#                             salary: lines.find(l => l.startsWith('$')) || '',
#                             location: lines.find(l => l.includes('•')) || '',
#                             url: link ? link.href : '',
#                         });
#                     });
#                 });
#                 return out;
#             }""")
#             await context.close()
#         results = _transform_wellfound_jobs(jobs_data[:params.max_results_per_source])
#     except Exception as exc:
#         console.print(f"[yellow]⚠ Wellfound scrape error: {exc}[/yellow]")
#     console.print(f"[dim]Wellfound: {len(results)} listings[/dim]")
#     return {"raw_results": results}


SOURCE_NODE_MAP = {
    "indeed": "scrape_indeed",
    "dice": "scrape_dice",
    "linkedin": "scrape_linkedin",
    "ziprecruiter": "scrape_ziprecruiter",
    "builtin": "scrape_builtin",
    # "wellfound": "scrape_wellfound",  # PARKED — IP-based bot detection blocks Playwright
    #   Wellfound returns "Access is temporarily restricted" citing automated activity
    #   from the IP. This is network-level, not session/cookie level — persistent
    #   browser profiles don't help. Potential workarounds to research:
    #   1. Residential proxy rotation
    #   2. Playwright stealth plugin (playwright-extra + puppeteer-extra-plugin-stealth)
    #   3. Official Wellfound API (requires partnership application)
    #   The _transform_wellfound_jobs() function and tests are preserved below
    #   so the plumbing is ready when a workaround is found.
    #   grep: WELLFOUND_PARKED to find all related code
}


def parse_search_params(state: PipelineState) -> dict:
    """Validate search_params. Warn if no sources configured."""
    warnings = list(state.get("warnings", []))
    if not state["search_params"].sources:
        warnings.append("No job sources configured — search will return no results.")
    return {"warnings": warnings}


def fan_out_sources(state: PipelineState) -> list[Send]:
    """Return a Send per enabled source. LangGraph runs all concurrently."""
    return [
        Send(SOURCE_NODE_MAP[src], state)
        for src in state["search_params"].sources
        if src in SOURCE_NODE_MAP
    ]


async def merge_results(state: PipelineState) -> dict:
    """
    Deduplicate raw_results by fingerprint. Cross-references DB to suppress
    listings the user has already handled (skipped, applied, submitted).
    Previously queued/draft listings are surfaced with a badge.
    """
    raw = state.get("raw_results", [])
    if not raw:
        console.print("[yellow]No raw results from any source.[/yellow]")
        return {"shortlist": [], "skipped": []}

    seen: dict[str, RawJobListing] = {}
    for listing in raw:
        fp = _make_fingerprint(listing.company, listing.title, listing.location)
        if fp not in seen:
            seen[fp] = listing

    suppress = {JobStatus.SKIPPED, JobStatus.APPLIED, JobStatus.SUBMITTED, JobStatus.OFFER}
    note_set = {JobStatus.QUEUED, JobStatus.DOCS_DRAFT, JobStatus.DOCS_READY}
    previously_seen: dict[str, JobStatus] = {}
    suppressed_company_roles: set[str] = set()
    seen_company_roles: dict[str, JobStatus] = {}
    try:
        async with get_connection() as conn:
            async for row in await conn.execute(
                "SELECT fingerprint, status, company, title FROM jobs WHERE fingerprint IN ({})".format(
                    ",".join("?" * len(seen))
                ),
                list(seen.keys()),
            ):
                status = JobStatus(row["status"])
                previously_seen[row["fingerprint"]] = status
                key = f"{(row['company'] or '').lower()}::{(row['title'] or '').lower()}"
                if status in suppress:
                    suppressed_company_roles.add(key)
                elif status in note_set:
                    seen_company_roles.setdefault(key, status)
    except Exception as exc:
        console.print(f"[yellow]⚠ DB lookup failed during merge: {exc}[/yellow]")

    shortlist: list[JobListing] = []
    for fp, raw_listing in seen.items():
        prior = previously_seen.get(fp)
        if prior in suppress:
            continue
        company_role_key = f"{raw_listing.company.lower()}::{raw_listing.title.lower()}"
        if company_role_key in suppressed_company_roles:
            continue
        job = JobListing(**raw_listing.model_dump(), fingerprint=fp, ats_type=detect_ats(raw_listing.apply_url))
        if prior in note_set:
            job.notes = f"[previously seen — status: {prior.value}]"
        elif company_role_key in seen_company_roles:
            cr_prior = seen_company_roles[company_role_key]
            job.notes = f"[previously seen — status: {cr_prior.value}]"
        shortlist.append(job)

    suppressed = len(seen) - len(shortlist)
    console.print(f"[bold]Merged:[/bold] {len(raw)} raw → {len(shortlist)} unique (suppressed {suppressed})")
    return {"shortlist": shortlist, "skipped": []}


async def triage_interrupt(state: PipelineState) -> dict:
    """
    Human-in-the-loop gate. Pauses the graph and surfaces the shortlist.
    CLI resumes with Command(resume={"apply": [...], "skip": [...]}).
    """
    shortlist = state.get("shortlist", [])
    if not shortlist:
        console.print("[yellow]No listings to triage.[/yellow]")
        return {}
    decisions: dict = interrupt({
        "shortlist": [j.model_dump(mode="json") for j in shortlist],
        "message": f"Review {len(shortlist)} listings and mark each: apply / skip",
    })
    apply_ids = set(decisions.get("apply", []))
    approved = [j for j in shortlist if j.id in apply_ids]
    new_skipped = [j for j in shortlist if j.id not in apply_ids]
    console.print(f"[green]Triage complete:[/green] {len(approved)} approved, {len(new_skipped)} skipped")
    return {"shortlist": approved, "skipped": state.get("skipped", []) + new_skipped}


async def persist_to_db(state: PipelineState) -> dict:
    """Write approved listings to jobs table with status=queued."""
    approved = state.get("shortlist", [])
    if not approved:
        return {}
    if state.get("dry_run"):
        console.print(
            f"DRY RUN — would save {len(approved)} jobs:", markup=False
        )
        for job in approved[:10]:
            console.print(f"  {job.company}::{job.title}", markup=False)
        return {}
    params = state["search_params"]
    run_id = str(uuid4())
    now = datetime.utcnow().isoformat()
    try:
        async with get_connection() as conn:
            for job in approved:
                await conn.execute(
                    """
                    INSERT INTO jobs (
                        id, title, company, location, workplace_type, source,
                        source_url, apply_url, ats_type, description,
                        compensation_low, compensation_high, posted_date,
                        discovered_at, status, fingerprint, notes
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        status = CASE
                            WHEN jobs.status IN ('skipped','applied','submitted','offer') THEN jobs.status
                            ELSE 'queued' END,
                        notes = excluded.notes
                    """,
                    (
                        job.id, job.title, job.company, job.location,
                        job.workplace_type.value if job.workplace_type else None,
                        job.source, job.source_url, job.apply_url, job.ats_type.value,
                        job.description, job.compensation_low, job.compensation_high,
                        job.posted_date.isoformat() if job.posted_date else None,
                        now, JobStatus.QUEUED.value, job.fingerprint, job.notes,
                    ),
                )
            await conn.execute(
                "INSERT INTO search_runs (id,run_at,params_json,sources_used,"
                "total_raw_results,total_shortlisted,total_approved,seen_job_ids) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (run_id, now, params.model_dump_json(), ",".join(params.sources),
                 len(state.get("raw_results", [])),
                 len(state.get("shortlist", [])) + len(state.get("skipped", [])),
                 len(approved), json.dumps([j.fingerprint for j in approved])),
            )
            await conn.commit()
    except Exception as exc:
        errors = list(state.get("errors", []))
        errors.append(f"persist_to_db failed: {exc}")
        console.print(f"[red]✗ DB persist error: {exc}[/red]")
        return {"errors": errors}
    console.print(f"[green]✓ {len(approved)} jobs saved (run: {run_id[:8]})[/green]")
    return {}


# ── Graph assembly ─────────────────────────────────────────────────────────────


def build_discoverer_graph() -> StateGraph:
    """
    Compile the Discoverer subgraph.

      parse_search_params
            │
      fan_out_sources ──(Send)──► scrape_indeed       ──┐
                      ──(Send)──► scrape_dice         ──┤
                      ──(Send)──► scrape_linkedin     ──┤──► merge_results
                      ──(Send)──► scrape_ziprecruiter ──┘         │
                                                           triage_interrupt
                                                                   │
                                                            persist_to_db
                                                                   │
                                                                  END
    """
    graph = StateGraph(PipelineState)

    graph.add_node("parse_search_params", parse_search_params)
    graph.add_node("scrape_indeed", scrape_indeed)
    graph.add_node("scrape_dice", scrape_dice)
    graph.add_node("scrape_linkedin", scrape_linkedin)
    graph.add_node("scrape_ziprecruiter", scrape_ziprecruiter)
    graph.add_node("scrape_builtin", scrape_builtin)
    # graph.add_node("scrape_wellfound", scrape_wellfound)  # WELLFOUND_PARKED
    graph.add_node("merge_results", merge_results)
    graph.add_node("triage_interrupt", triage_interrupt)
    graph.add_node("persist_to_db", persist_to_db)

    graph.set_entry_point("parse_search_params")
    graph.add_conditional_edges("parse_search_params", fan_out_sources)
    for scraper in ("scrape_indeed", "scrape_dice", "scrape_linkedin", "scrape_ziprecruiter", "scrape_builtin"):
        # "scrape_wellfound" omitted — WELLFOUND_PARKED
        graph.add_edge(scraper, "merge_results")
    graph.add_edge("merge_results", "triage_interrupt")
    graph.add_edge("triage_interrupt", "persist_to_db")
    graph.add_edge("persist_to_db", END)

    return graph
