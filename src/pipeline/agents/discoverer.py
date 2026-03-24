"""
Discoverer Agent — Phase 1 implementation target.

Responsibility: Search all four job sources, deduplicate results, present
a triage shortlist to the user, and persist approved listings to the DB.

LangGraph patterns exercised here:
  - Send API for parallel fan-out across job sources
  - interrupt() for the triage human-in-the-loop gate

Current state: Phase 0 stubs. Nodes are defined and wired but return
empty / pass-through state. Replace node bodies in Phase 1.
"""

from __future__ import annotations

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from pipeline.state import PipelineState


# ── Node stubs ─────────────────────────────────────────────────────────────────


def parse_search_params(state: PipelineState) -> dict:
    """Validate and normalize CLI input into SearchParams. Phase 1 target."""
    return {}


def fan_out_sources(state: PipelineState) -> dict:
    """
    Emit Send() for each enabled source to run scrapers in parallel.
    Phase 1 target — exercises the LangGraph Send API.
    """
    return {}


def scrape_indeed(state: PipelineState) -> dict:
    """httpx + BS4 scraper for Indeed. No auth required. Phase 1 target."""
    return {"raw_results": []}


def scrape_dice(state: PipelineState) -> dict:
    """httpx + BS4 scraper for Dice. No auth required. Phase 1 target."""
    return {"raw_results": []}


def scrape_linkedin(state: PipelineState) -> dict:
    """
    Playwright scraper for LinkedIn. Requires persistent auth session.
    Phase 2 target — Playwright auth sessions research required first.
    """
    return {"raw_results": []}


def scrape_ziprecruiter(state: PipelineState) -> dict:
    """
    Playwright scraper for ZipRecruiter. Requires persistent auth session.
    Phase 2 target — Playwright auth sessions research required first.
    """
    return {"raw_results": []}


def merge_results(state: PipelineState) -> dict:
    """
    Deduplicate by (company, title, location) fingerprint and normalize fields.
    Phase 1 target.
    """
    return {"shortlist": [], "skipped": []}


def enrich_listings(state: PipelineState) -> dict:
    """
    For shortlisted results: fetch full description, company headcount,
    ATS fingerprint. Phase 1 target.
    """
    return {}


def triage_interrupt(state: PipelineState) -> dict:
    """
    Human-in-the-loop gate: present shortlist, wait for apply/skip/save per listing.
    Uses LangGraph interrupt() — state is checkpointed here. Phase 1 target.
    """
    interrupt("Review shortlist and mark each listing: apply / skip / save")
    return {}


def persist_to_db(state: PipelineState) -> dict:
    """Write approved listings to jobs table with status=queued. Phase 1 target."""
    return {}


# ── Graph assembly ─────────────────────────────────────────────────────────────


def build_discoverer_graph() -> StateGraph:
    """
    Compile the Discoverer subgraph.

    Phase 0: linear stub chain — no parallel fan-out yet.
    Phase 1: replace fan_out_sources → scraper nodes with Send API.
    """
    graph = StateGraph(PipelineState)

    graph.add_node("parse_search_params", parse_search_params)
    graph.add_node("fan_out_sources", fan_out_sources)
    graph.add_node("scrape_indeed", scrape_indeed)
    graph.add_node("scrape_dice", scrape_dice)
    graph.add_node("scrape_linkedin", scrape_linkedin)
    graph.add_node("scrape_ziprecruiter", scrape_ziprecruiter)
    graph.add_node("merge_results", merge_results)
    graph.add_node("enrich_listings", enrich_listings)
    graph.add_node("triage_interrupt", triage_interrupt)
    graph.add_node("persist_to_db", persist_to_db)

    # Phase 0: simple linear chain (parallel fan-out wired in Phase 1)
    graph.set_entry_point("parse_search_params")
    graph.add_edge("parse_search_params", "fan_out_sources")
    graph.add_edge("fan_out_sources", "scrape_indeed")
    graph.add_edge("scrape_indeed", "scrape_dice")
    graph.add_edge("scrape_dice", "scrape_linkedin")
    graph.add_edge("scrape_linkedin", "scrape_ziprecruiter")
    graph.add_edge("scrape_ziprecruiter", "merge_results")
    graph.add_edge("merge_results", "enrich_listings")
    graph.add_edge("enrich_listings", "triage_interrupt")
    graph.add_edge("triage_interrupt", "persist_to_db")
    graph.add_edge("persist_to_db", END)

    return graph
