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
    """
    Query the DB for all jobs with non-terminal statuses and populate shortlist.
    Also loads submission follow-up data into state for use by render_dashboard
    and flag_overdue.

    Active statuses: new, queued, docs_draft, docs_ready, submitted, applied,
    possibly_inactive. Skips: rejected, offer, skipped.

    Uses sync sqlite3 — same pattern as writer and submitter nodes.
    """
    import sqlite3

    active_statuses = (
        "new", "queued", "docs_draft", "docs_ready",
        "submitted", "applied", "possibly_inactive",
    )
    placeholders = ",".join("?" * len(active_statuses))

    db_path = str(settings.app_db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY discovered_at DESC",
            active_statuses,
        ).fetchall()
        sub_rows = conn.execute(
            """
            SELECT s.job_id, s.followup_due_date, s.submitted_at
              FROM submissions s
              JOIN jobs j ON j.id = s.job_id
             WHERE j.status IN ('submitted', 'applied')
            """
        ).fetchall()
    finally:
        conn.close()

    jobs = [
        JobListing.model_validate({
            "id": r["id"],
            "title": r["title"],
            "company": r["company"],
            "location": r["location"],
            "workplace_type": r["workplace_type"],
            "source": r["source"],
            "source_url": r["source_url"],
            "apply_url": r["apply_url"],
            "ats_type": r["ats_type"] or "unknown",
            "description": r["description"],
            "compensation_low": r["compensation_low"],
            "compensation_high": r["compensation_high"],
            "posted_date": r["posted_date"],
            "discovered_at": r["discovered_at"],
            "status": r["status"],
            "fit_signal": r["fit_signal"],
            "company_headcount": r["company_headcount"],
            "fingerprint": r["fingerprint"] or "",
            "resume_path": r["resume_path"],
            "cover_letter_path": r["cover_letter_path"],
            "notes": r["notes"],
        })
        for r in rows
    ]

    submissions = [
        {
            "job_id": r["job_id"],
            "followup_due_date": r["followup_due_date"],
            "submitted_at": r["submitted_at"],
        }
        for r in sub_rows
    ]

    return {
        "shortlist": state.get("shortlist", []) + jobs,
        "tracker_submissions": submissions,
    }


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
    Advance a job's status in the DB and record updated_at.

    Reads current_job_id and tracker_new_status from state.
    If either is absent, returns without writing (no-op).
    All transitions are user-driven — no guards or validation applied here.
    """
    import sqlite3

    job_id = state.get("current_job_id")
    new_status = state.get("tracker_new_status")

    if not job_id or not new_status:
        return {}

    db_path = str(settings.app_db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE jobs SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (new_status, job_id),
        )
        conn.commit()
    except Exception as exc:
        return {"errors": state.get("errors", []) + [f"update_status: {exc}"]}
    finally:
        conn.close()

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
