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

    # Try authenticated DOM first (logged-in search results)
    auth_cards = soup.select("div.job-card-container")
    if auth_cards:
        for card in auth_cards:
            title_el = card.select_one("a.job-card-container__link span[aria-hidden='true']") \
                       or card.select_one(".job-card-list__title--link span[aria-hidden='true']") \
                       or card.select_one("a.job-card-container__link")
            company_el = card.select_one(".job-card-container__primary-description, .artdeco-entity-lockup__subtitle span")
            location_el = card.select_one(".job-card-container__metadata-item, .job-card-container__metadata-wrapper li")
            link_el = card.select_one("a.job-card-container__link, a.job-card-list__title")
            salary_el = card.select_one(".job-card-container__salary-info, .compensation-info")
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
    Parse ZipRecruiter job search results HTML into RawJobListing objects.
    Pure function — no browser dependency, fully unit testable.
    """
    soup = BeautifulSoup(html, "html.parser")
    results: list[RawJobListing] = []
    for card in soup.select("article.job_result"):
        title_el = card.select_one("h2.job_title a.job_link")
        company_el = card.select_one("a.company_name")
        location_el = card.select_one("span.location")
        salary_el = card.select_one("span.compensation")
        if not (title_el and company_el):
            continue
        title = title_el.get_text(strip=True)
        company = company_el.get_text(strip=True)
        location = location_el.get_text(strip=True) if location_el else ""
        href = title_el.get("href", "")
        source_url = (
            f"https://www.ziprecruiter.com{href}" if href.startswith("/") else href
        )
        comp_low, comp_high = _parse_compensation(
            salary_el.get_text(strip=True) if salary_el else ""
        )
        loc_lower = location.lower()
        workplace = (
            WorkplaceType.REMOTE if "remote" in loc_lower
            else WorkplaceType.HYBRID if "hybrid" in loc_lower
            else None
        )
        results.append(RawJobListing(
            source="ziprecruiter",
            title=title,
            company=company,
            location=location.replace("(Remote)", "").replace("(Hybrid)", "").strip(),
            source_url=source_url,
            compensation_low=comp_low,
            compensation_high=comp_high,
            workplace_type=workplace,
        ))
    return results


# ── Indeed scraper (httpx + BS4, no auth) ─────────────────────────────────────


async def scrape_indeed(state: PipelineState) -> dict:
    """
    Scrape Indeed via Playwright (headless=False, no auth).
    httpx returns 403 — a real browser bypasses Indeed's bot detection.
    The existing BS4 card selectors are reused unchanged.
    """
    if async_playwright is None:
        console.print("[yellow]⚠ Playwright not available — skipping Indeed[/yellow]")
        return {"raw_results": []}

    params = state["search_params"]
    query_str = params.query + (" remote" if params.remote else "")
    url = (
        f"https://www.indeed.com/jobs"
        f"?q={quote_plus(query_str)}"
        f"&l={quote_plus(params.location)}"
        f"&sort=date&limit={params.max_results_per_source}"
    )
    results: list[RawJobListing] = []
    try:
        import asyncio as _asyncio
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, channel="chrome")
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded")
            await _asyncio.sleep(3)  # let JS render job cards
            # Wait for at least one card selector to appear
            for sel in ("div.job_seen_beacon", "div.tapItem", "li.css-5lfssm"):
                try:
                    await page.wait_for_selector(sel, timeout=5000)
                    break
                except Exception:
                    continue
            html = await page.content()
            await browser.close()
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("li.css-5lfssm, div.job_seen_beacon, div.tapItem")
        for card in cards[:params.max_results_per_source]:
            title_el = card.select_one("h2.jobTitle span[title], h2.jobTitle a span")
            company_el = card.select_one("span.companyName, [data-testid='company-name']")
            location_el = card.select_one("div.companyLocation, [data-testid='text-location']")
            link_el = card.select_one("a[id^='job_'], a.jcs-JobTitle, h2.jobTitle a")
            salary_el = card.select_one("div.metadata.salary-snippet-container")
            if not (title_el and company_el):
                continue
            title = title_el.get("title") or title_el.get_text(strip=True)
            company = company_el.get_text(strip=True)
            location = location_el.get_text(strip=True) if location_el else params.location
            href = link_el.get("href", "") if link_el else ""
            source_url = f"https://www.indeed.com{href}" if href.startswith("/") else href
            comp_low, comp_high = _parse_compensation(
                salary_el.get_text(strip=True) if salary_el else ""
            )
            results.append(RawJobListing(
                source="indeed", title=title, company=company, location=location,
                source_url=source_url or url, compensation_low=comp_low,
                compensation_high=comp_high,
                workplace_type=WorkplaceType.REMOTE if "remote" in location.lower() else None,
            ))
    except Exception as exc:
        console.print(f"[yellow]⚠ Indeed scrape error: {exc}[/yellow]")
    console.print(f"[dim]Indeed: {len(results)} listings[/dim]")
    return {"raw_results": results}


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
            comp_low, comp_high = _parse_compensation(salary_str)
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
    Scrape ZipRecruiter job search via Playwright with a saved auth session.
    Windows only. Requires playwright/.auth/ziprecruiter.json from save_auth.py.
    """
    auth_path = _get_auth_path("ziprecruiter")
    if not auth_path.exists():
        console.print(
            "[yellow]⚠ ZipRecruiter auth file not found. "
            "Run: python scripts/save_auth.py --platform ziprecruiter[/yellow]"
        )
        return {"raw_results": []}

    params = state["search_params"]
    search_url = (
        "https://www.ziprecruiter.com/jobs-search"
        f"?search={quote_plus(params.query)}"
        f"&location={quote_plus(params.location)}"
    )
    results: list[RawJobListing] = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel="chrome")
            context = await browser.new_context(storage_state=str(auth_path))
            page = await context.new_page()
            await page.goto(search_url, wait_until="domcontentloaded")
            await page.wait_for_selector("article.job_result", timeout=10000)
            if _is_login_redirect(page.url, "ziprecruiter"):
                console.print(
                    "[yellow]⚠ ZipRecruiter session expired. "
                    "Run: python scripts/save_auth.py --platform ziprecruiter[/yellow]"
                )
                await browser.close()
                return {"raw_results": []}
            html = await page.content()
            await browser.close()
        results = _parse_ziprecruiter_cards(html)
    except Exception as exc:
        console.print(f"[yellow]⚠ ZipRecruiter scrape error: {exc}[/yellow]")
    console.print(f"[dim]ZipRecruiter: {len(results)} listings[/dim]")
    return {"raw_results": results}


# ── Graph nodes ────────────────────────────────────────────────────────────────


SOURCE_NODE_MAP = {
    "indeed": "scrape_indeed",
    "dice": "scrape_dice",
    "linkedin": "scrape_linkedin",
    "ziprecruiter": "scrape_ziprecruiter",
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
    previously_seen: dict[str, JobStatus] = {}
    try:
        async with get_connection() as conn:
            async for row in await conn.execute(
                "SELECT fingerprint, status FROM jobs WHERE fingerprint IN ({})".format(
                    ",".join("?" * len(seen))
                ),
                list(seen.keys()),
            ):
                previously_seen[row["fingerprint"]] = JobStatus(row["status"])
    except Exception as exc:
        console.print(f"[yellow]⚠ DB lookup failed during merge: {exc}[/yellow]")

    shortlist: list[JobListing] = []
    for fp, raw_listing in seen.items():
        prior = previously_seen.get(fp)
        if prior in suppress:
            continue
        job = JobListing(**raw_listing.model_dump(), fingerprint=fp, ats_type=detect_ats(raw_listing.apply_url))
        if prior in (JobStatus.QUEUED, JobStatus.DOCS_DRAFT, JobStatus.DOCS_READY):
            job.notes = f"[previously seen — status: {prior.value}]"
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
    graph.add_node("merge_results", merge_results)
    graph.add_node("triage_interrupt", triage_interrupt)
    graph.add_node("persist_to_db", persist_to_db)

    graph.set_entry_point("parse_search_params")
    graph.add_conditional_edges("parse_search_params", fan_out_sources)
    for scraper in ("scrape_indeed", "scrape_dice", "scrape_linkedin", "scrape_ziprecruiter"):
        graph.add_edge(scraper, "merge_results")
    graph.add_edge("merge_results", "triage_interrupt")
    graph.add_edge("triage_interrupt", "persist_to_db")
    graph.add_edge("persist_to_db", END)

    return graph
