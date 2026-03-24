"""
Discoverer Agent — Phase 1 implementation.

Responsibility:
  Search Indeed and Dice (no-auth), deduplicate by fingerprint, cross-
  reference the DB to suppress already-seen listings, present a triage
  shortlist to the user, and persist approved listings to the jobs table.

LangGraph patterns exercised here:
  - Send API (via add_conditional_edges + route_to_scrapers) for parallel
    fan-out across job sources. Each scraper runs concurrently; their
    raw_results are accumulated via the operator.add reducer on PipelineState.
  - interrupt() for the triage human-in-the-loop gate. The graph checkpoints
    at this boundary; the CLI resumes via Command(resume=decisions).

Phase 2 targets (stubs in this file):
  - scrape_linkedin  — Playwright + persistent browser profile
  - scrape_ziprecruiter — Playwright + persistent browser profile
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from urllib.parse import quote_plus
from uuid import uuid4

import httpx
from bs4 import BeautifulSoup
from langgraph.graph import END, StateGraph
from langgraph.types import Send, interrupt
from rich.console import Console
from rich.table import Table
from rich import box

from pipeline.database import get_connection
from pipeline.state import (
    AtsType,
    JobListing,
    JobStatus,
    PipelineState,
    RawJobListing,
    WorkplaceType,
)

console = Console()

# ── HTTP headers ───────────────────────────────────────────────────────────────
# Realistic browser headers reduce 403 rate on public job boards.
# Indeed still blocks aggressively; graceful fallback is built in.

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

# ── ATS fingerprinting ─────────────────────────────────────────────────────────

ATS_PATTERNS: dict[str, list[str]] = {
    "greenhouse": ["greenhouse.io", "boards.greenhouse.io"],
    "workday": ["workday.com", "myworkdayjobs.com"],
    "ashby": ["ashbyhq.com", "jobs.ashby"],
    "linkedin": ["linkedin.com/jobs"],
}


def _detect_ats(url: str | None) -> AtsType:
    """Fingerprint an apply URL to determine the ATS type."""
    if not url:
        return AtsType.UNKNOWN
    url_lower = url.lower()
    for ats, patterns in ATS_PATTERNS.items():
        if any(p in url_lower for p in patterns):
            return AtsType(ats)
    return AtsType.OTHER


def _make_fingerprint(company: str, title: str, location: str) -> str:
    """Stable 16-char hash of (company, title, location) for dedup."""
    key = f"{company.lower().strip()}|{title.lower().strip()}|{location.lower().strip()}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _parse_compensation(text: str) -> tuple[int | None, int | None]:
    """
    Extract annualized salary range from a string like '$120K - $160K/yr'
    or '$60/hr'. Returns (low, high) in whole dollars. Returns (None, None)
    if no salary pattern is found.
    """
    text = text.replace(",", "")
    # Hourly: $45/hr → annualize at 2080 hrs/yr
    hourly = re.search(r"\$(\d+(?:\.\d+)?)\s*/\s*hr", text, re.IGNORECASE)
    if hourly:
        rate = float(hourly.group(1))
        return int(rate * 2080), int(rate * 2080)
    # Range with K: $120K - $160K
    range_k = re.findall(r"\$(\d+(?:\.\d+)?)K", text, re.IGNORECASE)
    if len(range_k) >= 2:
        return int(float(range_k[0]) * 1000), int(float(range_k[1]) * 1000)
    if len(range_k) == 1:
        val = int(float(range_k[0]) * 1000)
        return val, val
    # Plain range: $120000 - $160000
    range_plain = re.findall(r"\$(\d{5,6})", text)
    if len(range_plain) >= 2:
        return int(range_plain[0]), int(range_plain[1])
    return None, None


# ── Indeed scraper ─────────────────────────────────────────────────────────────


async def scrape_indeed(state: PipelineState) -> dict:
    """
    Scrape Indeed job search results using httpx + BeautifulSoup.

    Indeed's public search endpoint is:
      https://www.indeed.com/jobs?q={query}&l={location}&sort=date&limit=25

    HTML structure targeted:
      - Job cards: <div class="job_seen_beacon"> or <li class="css-...">
      - Title: <span title="..."> inside <h2 class="jobTitle">
      - Company: <span class="companyName">
      - Location: <div class="companyLocation">
      - Salary: <div class="metadata salary-snippet-container">
      - Job link: <a id="job_..."> href attribute

    Indeed blocks scrapers aggressively. On 403/429 we log a warning and
    return empty results rather than crashing the pipeline.
    """
    params = state["search_params"]
    query_str = params.query
    if params.remote:
        query_str += " remote"
    url = (
        f"https://www.indeed.com/jobs"
        f"?q={quote_plus(query_str)}"
        f"&l={quote_plus(params.location)}"
        f"&sort=date&limit={params.max_results_per_source}"
    )
    results: list[RawJobListing] = []
    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15) as client:
            resp = await client.get(url)
        if resp.status_code in (403, 429):
            console.print(f"[yellow]⚠ Indeed blocked scrape (HTTP {resp.status_code}) — skipping[/yellow]")
            return {"raw_results": []}
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("li.css-5lfssm, div.job_seen_beacon, div.tapItem")
        for card in cards[:params.max_results_per_source]:
            title_el = card.select_one("h2.jobTitle span[title], h2.jobTitle a span")
            company_el = card.select_one("span.companyName, [data-testid='company-name']")
            location_el = card.select_one("div.companyLocation, [data-testid='text-location']")
            link_el = card.select_one("a[id^='job_'], a.jcs-JobTitle, h2.jobTitle a")
            salary_el = card.select_one("div.metadata.salary-snippet-container, div.salaryOnly")
            if not (title_el and company_el):
                continue
            title = title_el.get("title") or title_el.get_text(strip=True)
            company = company_el.get_text(strip=True)
            location = location_el.get_text(strip=True) if location_el else params.location
            href = link_el.get("href", "") if link_el else ""
            source_url = f"https://www.indeed.com{href}" if href.startswith("/") else href
            salary_text = salary_el.get_text(strip=True) if salary_el else ""
            comp_low, comp_high = _parse_compensation(salary_text)
            workplace = WorkplaceType.REMOTE if "remote" in location.lower() else None
            results.append(RawJobListing(
                source="indeed",
                title=title,
                company=company,
                location=location,
                source_url=source_url or url,
                compensation_low=comp_low,
                compensation_high=comp_high,
                workplace_type=workplace,
            ))
    except Exception as exc:
        console.print(f"[yellow]⚠ Indeed scrape error: {exc}[/yellow]")
    console.print(f"[dim]Indeed: {len(results)} listings[/dim]")
    return {"raw_results": results}


# ── Dice scraper ───────────────────────────────────────────────────────────────


async def scrape_dice(state: PipelineState) -> dict:
    """
    Scrape Dice job search results using httpx + BeautifulSoup.

    Dice provides a JSON-backed search API at:
      https://job-search-api.svc.dhigroupinc.com/v1/dice/jobs/search
      ?q={query}&location={location}&countryCode=US&radius=30&radiusUnit=mi
      &page=1&pageSize=20&filters.postedDate=ONE_WEEK&language=en

    This is the API the Dice frontend calls; it's stable and returns
    structured JSON, making it more reliable than HTML scraping.
    """
    params = state["search_params"]
    api_url = (
        "https://job-search-api.svc.dhigroupinc.com/v1/dice/jobs/search"
        f"?q={quote_plus(params.query)}"
        f"&location={quote_plus(params.location)}"
        f"&countryCode=US&radius=30&radiusUnit=mi"
        f"&page=1&pageSize={params.max_results_per_source}"
        f"&language=en"
    )
    results: list[RawJobListing] = []
    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15) as client:
            resp = await client.get(api_url)
        if resp.status_code in (403, 429):
            console.print(f"[yellow]⚠ Dice blocked (HTTP {resp.status_code}) — skipping[/yellow]")
            return {"raw_results": []}
        resp.raise_for_status()
        data = resp.json()
        for job in data.get("data", []):
            location_obj = job.get("location", {})
            location_str = (
                f"{location_obj.get('city', '')}, {location_obj.get('state', '')}".strip(", ")
                or params.location
            )
            apply_url = job.get("applyUrl") or job.get("externalApplyLink")
            comp_low, comp_high = _parse_compensation(
                job.get("salaryRange", "") or job.get("compensationRange", "") or ""
            )
            workplace_str = (job.get("workplaceTypes") or [""])[0].lower()
            workplace = (
                WorkplaceType.REMOTE if "remote" in workplace_str
                else WorkplaceType.HYBRID if "hybrid" in workplace_str
                else WorkplaceType.ONSITE if "onsite" in workplace_str or "on-site" in workplace_str
                else None
            )
            job_id = job.get("id", "")
            source_url = f"https://www.dice.com/job-detail/{job_id}" if job_id else api_url
            results.append(RawJobListing(
                source="dice",
                title=job.get("title", "Unknown"),
                company=job.get("companyPageUrl", job.get("advertiserName", "Unknown")),
                location=location_str,
                source_url=source_url,
                apply_url=apply_url,
                description=job.get("jobDescription"),
                compensation_low=comp_low,
                compensation_high=comp_high,
                workplace_type=workplace,
            ))
    except Exception as exc:
        console.print(f"[yellow]⚠ Dice scrape error: {exc}[/yellow]")
    console.print(f"[dim]Dice: {len(results)} listings[/dim]")
    return {"raw_results": results}


# ── Phase 2 stubs (Playwright / auth required) ────────────────────────────────


async def scrape_linkedin(state: PipelineState) -> dict:
    """
    Playwright scraper for LinkedIn. Requires persistent auth session.
    Phase 2 target — see research backlog in PRD for storage_state approach.
    """
    console.print("[dim]LinkedIn scraper: Phase 2 stub — skipping[/dim]")
    return {"raw_results": []}


async def scrape_ziprecruiter(state: PipelineState) -> dict:
    """
    Playwright scraper for ZipRecruiter. Requires persistent auth session.
    Phase 2 target — see research backlog in PRD for storage_state approach.
    """
    console.print("[dim]ZipRecruiter scraper: Phase 2 stub — skipping[/dim]")
    return {"raw_results": []}


# ── Routing node (Send API fan-out) ───────────────────────────────────────────


SOURCE_NODE_MAP = {
    "indeed": "scrape_indeed",
    "dice": "scrape_dice",
    "linkedin": "scrape_linkedin",
    "ziprecruiter": "scrape_ziprecruiter",
}


def parse_search_params(state: PipelineState) -> dict:
    """
    Validate search_params are present and non-empty.
    Adds a warning if no sources are configured; the graph can still run
    (merge_results will receive empty raw_results and shortlist accordingly).
    """
    params = state["search_params"]
    warnings = list(state.get("warnings", []))
    if not params.sources:
        warnings.append("No job sources configured — search will return no results.")
    return {"warnings": warnings}


def fan_out_sources(state: PipelineState) -> list[Send]:
    """
    Emit a Send for each enabled source. LangGraph runs all branches
    concurrently; raw_results are accumulated via the operator.add reducer.
    """
    sources = state["search_params"].sources
    return [Send(SOURCE_NODE_MAP[src], state) for src in sources if src in SOURCE_NODE_MAP]


# ── Merge and dedup ────────────────────────────────────────────────────────────


async def merge_results(state: PipelineState) -> dict:
    """
    Deduplicate raw_results by (company, title, location) fingerprint.
    Cross-references the DB to suppress listings the user has already seen:
      - skipped / applied / submitted → suppress silently
      - queued / docs_draft → surface with a 'previously seen' badge (via notes)
      - new (never triaged) → treat as fresh

    Returns normalized JobListing objects in shortlist.
    """
    raw = state.get("raw_results", [])
    if not raw:
        console.print("[yellow]No raw results from any source.[/yellow]")
        return {"shortlist": [], "skipped": []}

    # Dedup within this run by fingerprint
    seen_fps: dict[str, RawJobListing] = {}
    for listing in raw:
        fp = _make_fingerprint(listing.company, listing.title, listing.location)
        if fp not in seen_fps:
            seen_fps[fp] = listing

    # Cross-reference DB for previously seen fingerprints
    suppress_statuses = {JobStatus.SKIPPED, JobStatus.APPLIED, JobStatus.SUBMITTED, JobStatus.OFFER}
    previously_seen: dict[str, JobStatus] = {}
    try:
        conn = await get_connection()
        async with conn:
            async for row in await conn.execute(
                "SELECT fingerprint, status FROM jobs WHERE fingerprint IN ({})".format(
                    ",".join("?" * len(seen_fps))
                ),
                list(seen_fps.keys()),
            ):
                previously_seen[row["fingerprint"]] = JobStatus(row["status"])
    except Exception as exc:
        console.print(f"[yellow]⚠ DB lookup failed during merge: {exc}[/yellow]")

    shortlist: list[JobListing] = []
    for fp, raw_listing in seen_fps.items():
        prior_status = previously_seen.get(fp)
        if prior_status in suppress_statuses:
            continue  # Already handled; suppress silently
        job = JobListing(
            **raw_listing.model_dump(),
            fingerprint=fp,
            ats_type=_detect_ats(raw_listing.apply_url),
        )
        if prior_status in (JobStatus.QUEUED, JobStatus.DOCS_DRAFT, JobStatus.DOCS_READY):
            job.notes = f"[previously seen — status: {prior_status.value}]"
        shortlist.append(job)

    console.print(f"[bold]Merged:[/bold] {len(raw)} raw → {len(shortlist)} unique (suppressed {len(seen_fps) - len(shortlist)})")
    return {"shortlist": shortlist, "skipped": []}


# ── Triage interrupt ───────────────────────────────────────────────────────────


async def triage_interrupt(state: PipelineState) -> dict:
    """
    Human-in-the-loop gate.

    Pauses the graph and surfaces the shortlist for user review. The CLI
    collects per-listing apply/skip decisions and resumes with:
      Command(resume={"apply": [job_id, ...], "skip": [job_id, ...]})

    The graph checkpoints at this boundary. If the process dies here, the
    user can resume the run the next day and this node re-presents the list.
    """
    shortlist = state.get("shortlist", [])
    if not shortlist:
        console.print("[yellow]No listings to triage — nothing to apply to.[/yellow]")
        return {}

    # interrupt() suspends execution and surfaces data to the caller.
    # The value passed here is what the CLI receives in GraphInterrupt.interrupts[0].value.
    decisions: dict = interrupt({
        "shortlist": [j.model_dump(mode="json") for j in shortlist],
        "message": f"Review {len(shortlist)} listings and mark each: apply / skip",
    })

    apply_ids = set(decisions.get("apply", []))
    skip_ids = set(decisions.get("skip", []))

    approved = [j for j in shortlist if j.id in apply_ids]
    new_skipped = [j for j in shortlist if j.id in skip_ids or j.id not in apply_ids]

    console.print(f"[green]Triage complete:[/green] {len(approved)} approved, {len(new_skipped)} skipped")
    return {
        "shortlist": approved,
        "skipped": state.get("skipped", []) + new_skipped,
    }


# ── Persist to DB ──────────────────────────────────────────────────────────────


async def persist_to_db(state: PipelineState) -> dict:
    """
    Write approved listings to the jobs table with status=queued.
    Also writes a search_run record for ghost-listing detection in future runs.
    On conflict (same fingerprint already in DB at a re-apply-eligible status),
    updates the record rather than inserting a duplicate.
    """
    approved = state.get("shortlist", [])
    params = state["search_params"]
    run_id = str(uuid4())
    now = datetime.utcnow().isoformat()

    if not approved:
        return {}

    try:
        conn = await get_connection()
        async with conn:
            for job in approved:
                await conn.execute(
                    """
                    INSERT INTO jobs (
                        id, title, company, location, workplace_type, source,
                        source_url, apply_url, ats_type, description,
                        compensation_low, compensation_high, posted_date,
                        discovered_at, status, fingerprint, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        status = CASE
                            WHEN jobs.status IN ('skipped','applied','submitted','offer')
                            THEN jobs.status
                            ELSE 'queued'
                        END,
                        notes = excluded.notes
                    """,
                    (
                        job.id, job.title, job.company, job.location,
                        job.workplace_type.value if job.workplace_type else None,
                        job.source, job.source_url, job.apply_url,
                        job.ats_type.value, job.description,
                        job.compensation_low, job.compensation_high,
                        job.posted_date.isoformat() if job.posted_date else None,
                        now, JobStatus.QUEUED.value, job.fingerprint, job.notes,
                    ),
                )
            fingerprints_json = json.dumps([j.fingerprint for j in approved])
            await conn.execute(
                """
                INSERT INTO search_runs
                  (id, run_at, params_json, sources_used,
                   total_raw_results, total_shortlisted, total_approved, seen_job_ids)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, now, params.model_dump_json(),
                    ",".join(params.sources),
                    len(state.get("raw_results", [])),
                    len(state.get("shortlist", [])) + len(state.get("skipped", [])),
                    len(approved),
                    fingerprints_json,
                ),
            )
            await conn.commit()
    except Exception as exc:
        errors = list(state.get("errors", []))
        errors.append(f"persist_to_db failed: {exc}")
        console.print(f"[red]✗ DB persist error: {exc}[/red]")
        return {"errors": errors}

    console.print(f"[green]✓ {len(approved)} jobs saved to DB (search run: {run_id[:8]})[/green]")
    return {}


# ── Graph assembly ─────────────────────────────────────────────────────────────


def build_discoverer_graph() -> StateGraph:
    """
    Compile the Discoverer subgraph.

    Topology:
      parse_search_params
            │
      fan_out_sources  ──(Send)──►  scrape_indeed   ──┐
                       ──(Send)──►  scrape_dice     ──┤
                       ──(Send)──►  scrape_linkedin ──┤─► merge_results
                       ──(Send)──►  scrape_ziprecruiter┘
                                                        │
                                                  triage_interrupt
                                                        │
                                                   persist_to_db
                                                        │
                                                       END

    The fan_out_sources node uses add_conditional_edges with a routing
    function that returns a list[Send] — one per enabled source. LangGraph
    runs all Send branches in parallel and accumulates raw_results via the
    operator.add reducer defined on PipelineState.
    """
    graph = StateGraph(PipelineState)

    graph.add_node("parse_search_params", parse_search_params)
    graph.add_node("fan_out_sources", fan_out_sources)
    graph.add_node("scrape_indeed", scrape_indeed)
    graph.add_node("scrape_dice", scrape_dice)
    graph.add_node("scrape_linkedin", scrape_linkedin)
    graph.add_node("scrape_ziprecruiter", scrape_ziprecruiter)
    graph.add_node("merge_results", merge_results)
    graph.add_node("triage_interrupt", triage_interrupt)
    graph.add_node("persist_to_db", persist_to_db)

    graph.set_entry_point("parse_search_params")
    graph.add_edge("parse_search_params", "fan_out_sources")

    # fan_out_sources returns list[Send] — LangGraph uses conditional edges
    # to dispatch to the appropriate scraper nodes in parallel.
    graph.add_conditional_edges("fan_out_sources", fan_out_sources)

    # All scraper nodes converge at merge_results
    for scraper in ("scrape_indeed", "scrape_dice", "scrape_linkedin", "scrape_ziprecruiter"):
        graph.add_edge(scraper, "merge_results")

    graph.add_edge("merge_results", "triage_interrupt")
    graph.add_edge("triage_interrupt", "persist_to_db")
    graph.add_edge("persist_to_db", END)

    return graph
