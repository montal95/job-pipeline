"""
Tracker Agent — Phase 5 implementation.

Responsibility: Status dashboard, follow-up scheduling, status updates.
Zero LLM calls — pure DB reads/writes and terminal rendering via rich.

LangGraph pattern exercised here:
  - Graph as workflow orchestrator for purely synchronous DB operations.
    Demonstrates that LangGraph is useful even without LLMs or async I/O.
"""

from __future__ import annotations

from datetime import date

from langgraph.graph import END, StateGraph

from pipeline.config import settings
from pipeline.state import JobListing, JobStatus, PipelineState


# ── Pure helper functions (unit testable — no DB, no Rich, no LangGraph) ──────


def _count_by_status(jobs: list[JobListing]) -> dict[str, int]:
    """Return a dict of status → count for a list of JobListings."""
    counts: dict[str, int] = {}
    for job in jobs:
        key = job.status.value if isinstance(job.status, JobStatus) else str(job.status)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _find_overdue(jobs: list[JobListing], submissions: list[dict]) -> list[str]:
    """
    Return job IDs where followup_due_date has passed and status is still 'applied'.

    submissions is a list of dicts with at least job_id and followup_due_date keys,
    matching the submissions table schema.
    """
    today = date.today().isoformat()
    applied_ids = {
        j.id for j in jobs
        if (j.status.value if isinstance(j.status, JobStatus) else str(j.status)) == "applied"
    }
    return [
        s["job_id"] for s in submissions
        if s["job_id"] in applied_ids
        and s.get("followup_due_date") is not None
        and s["followup_due_date"] < today
    ]


def _format_days_since(date_str: str) -> str:
    """
    Return a human-readable relative date string given an ISO date string.
    Examples: 'today', '1 day ago', '7 days ago'.
    Only the date portion (YYYY-MM-DD) is used; time components are ignored.
    """
    delta = (date.today() - date.fromisoformat(date_str[:10])).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "1 day ago"
    return f"{delta} days ago"


# ── Node stubs ─────────────────────────────────────────────────────────────────


def load_pipeline(state: PipelineState) -> dict:
    """Query DB for all jobs with active statuses. Phase 5 target."""
    return {}


def render_dashboard(state: PipelineState) -> dict:
    """
    Print a rich terminal table:
      - Status counts (queued, applied, rejected, offer, possibly_inactive)
      - Follow-up due dates
      - Pending reviews
      - Ghost listings flagged by detection logic

    Phase 5 target.
    """
    return {}


def update_status(state: PipelineState) -> dict:
    """
    Accept CLI args or interactive prompts to move a job's status.
    All status transitions require user intent — nothing auto-updates.
    Phase 5 target.
    """
    return {}


def schedule_followup(state: PipelineState) -> dict:
    """
    Compute and write follow-up date (applied_date + N days) to submissions table.
    N is configurable per job or defaults to a sensible interval.
    Phase 5 target.
    """
    return {}


def flag_overdue(state: PipelineState) -> dict:
    """
    Surface jobs where follow-up date has passed with no status update.
    Also applies ghost listing detection logic (no-show in last N discovery runs).
    Phase 5 target.
    """
    return {}


# ── Graph assembly ─────────────────────────────────────────────────────────────


def build_tracker_graph() -> StateGraph:
    graph = StateGraph(PipelineState)

    graph.add_node("load_pipeline", load_pipeline)
    graph.add_node("render_dashboard", render_dashboard)
    graph.add_node("update_status", update_status)
    graph.add_node("schedule_followup", schedule_followup)
    graph.add_node("flag_overdue", flag_overdue)

    graph.set_entry_point("load_pipeline")
    graph.add_edge("load_pipeline", "render_dashboard")
    graph.add_edge("render_dashboard", "flag_overdue")
    graph.add_edge("flag_overdue", "update_status")
    graph.add_edge("update_status", "schedule_followup")
    graph.add_edge("schedule_followup", END)

    return graph
