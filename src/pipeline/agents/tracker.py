"""
Tracker Agent — Phase 5 implementation.

Responsibility: Status dashboard, follow-up scheduling, overdue detection,
and user-driven status updates. Zero LLM calls — pure DB reads/writes and
Rich terminal rendering.

LangGraph pattern exercised here:
  - Graph as workflow orchestrator for purely synchronous operations.
    The Tracker has no LLMs, no async I/O, and no interrupt gates — it is a
    straight linear graph over SQLite reads and Rich terminal output. This
    demonstrates that LangGraph is useful as a sequencing tool even when AI
    is not involved: the node order encodes causal dependencies (schedule
    before check; check before render) that would otherwise live in prose.

Node order and why it matters:
  load_pipeline → schedule_followup → flag_overdue → render_dashboard → update_status

  schedule_followup must precede flag_overdue: it writes followup_due_date
  values that flag_overdue then reads to determine what is overdue.
  flag_overdue must precede render_dashboard: it appends warnings that the
  dashboard uses to render ⚠ overdue indicators.
  update_status is last and is a no-op if tracker_new_status is not set in
  state — safe to always run at the end of the graph.

Pure functions vs. node implementations:
  _count_by_status, _find_overdue, _format_days_since are pure Python with
  no DB or terminal dependencies — fully unit-testable. render_dashboard,
  schedule_followup, and flag_overdue are not unit-tested for the same reason
  as Playwright nodes: their outputs are terminal side-effects or DB writes
  that are integration-level concerns, not unit concerns.
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


# ── Nodes ─────────────────────────────────────────────────────────────────────


def load_pipeline(state: PipelineState) -> dict:
    """
    Query the DB for all jobs with non-terminal statuses and populate shortlist.
    Also loads submission follow-up data into state for render_dashboard and
    flag_overdue.

    Active statuses: new, queued, docs_draft, docs_ready, submitted, applied,
    possibly_inactive. Terminal statuses skipped: rejected, offer, skipped.
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
            "id": r["id"], "title": r["title"], "company": r["company"],
            "location": r["location"], "workplace_type": r["workplace_type"],
            "source": r["source"], "source_url": r["source_url"],
            "apply_url": r["apply_url"], "ats_type": r["ats_type"] or "unknown",
            "description": r["description"],
            "compensation_low": r["compensation_low"],
            "compensation_high": r["compensation_high"],
            "posted_date": r["posted_date"], "discovered_at": r["discovered_at"],
            "status": r["status"], "fit_signal": r["fit_signal"],
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


def schedule_followup(state: PipelineState) -> dict:
    """
    For any submitted/applied job with no followup_due_date, write
    submitted_at + 7 days to the submissions table.
    Idempotent — skips rows that already have a followup_due_date set.
    """
    import sqlite3

    db_path = str(settings.app_db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            UPDATE submissions
               SET followup_due_date = date(submitted_at, '+7 days')
             WHERE followup_due_date IS NULL
               AND job_id IN (
                   SELECT id FROM jobs WHERE status IN ('submitted', 'applied')
               )
            """
        )
        conn.commit()
    except Exception as exc:
        return {"errors": state.get("errors", []) + [f"schedule_followup: {exc}"]}
    finally:
        conn.close()
    return {}


def flag_overdue(state: PipelineState) -> dict:
    """
    Find jobs where followup_due_date has passed and status is still 'applied'.
    Appends a warning per overdue job for render_dashboard to surface.
    """
    jobs = state.get("shortlist", [])
    submissions = state.get("tracker_submissions", [])
    overdue_ids = _find_overdue(jobs, submissions)

    if not overdue_ids:
        return {}

    id_to_job = {j.id: j for j in jobs}
    warnings = list(state.get("warnings", []))
    for job_id in overdue_ids:
        job = id_to_job.get(job_id)
        label = f"{job.company} — {job.title}" if job else job_id
        warnings.append(f"Follow-up overdue: {label}")
    return {"warnings": warnings}


def render_dashboard(state: PipelineState) -> dict:
    """
    Render a two-table Rich terminal dashboard.
    Table 1: status counts. Table 2: active applications with overdue indicator.
    Rich output is not unit-tested (same rationale as Playwright nodes).
    """
    from rich import box as rich_box
    from rich.console import Console
    from rich.table import Table

    console = Console()
    jobs = state.get("shortlist", [])
    submissions = state.get("tracker_submissions", [])
    overdue_ids = set(_find_overdue(jobs, submissions))
    counts = _count_by_status(jobs)

    summary = Table(title="Pipeline Summary", box=rich_box.SIMPLE_HEAVY, show_header=True)
    status_order = ["new", "queued", "docs_ready", "applied", "submitted", "rejected", "offer"]
    for s in status_order:
        summary.add_column(s.upper(), justify="center", style="cyan")
    summary.add_row(*[str(counts.get(s, 0)) for s in status_order])
    console.print(summary)

    sub_map = {s["job_id"]: s for s in submissions}
    active = [j for j in jobs if str(j.status) in ("applied", "submitted")]
    if active:
        apps = Table(title="Active Applications", box=rich_box.SIMPLE, show_header=True)
        apps.add_column("Company", style="bold")
        apps.add_column("Role")
        apps.add_column("Applied")
        apps.add_column("Follow-up")
        apps.add_column("Status")
        for job in active:
            sub = sub_map.get(job.id, {})
            applied_str = (
                _format_days_since(sub["submitted_at"][:10])
                if sub.get("submitted_at") else "—"
            )
            followup = sub.get("followup_due_date") or "—"
            indicator = "⚠ overdue" if job.id in overdue_ids else "ok"
            apps.add_row(job.company, job.title, applied_str, followup, indicator)
        console.print(apps)
    return {}


def update_status(state: PipelineState) -> dict:
    """
    Advance a job's status in the DB and record updated_at.
    Reads current_job_id and tracker_new_status from state.
    No-op if either is absent — safe to always run at end of graph.
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


# ── Graph assembly ─────────────────────────────────────────────────────────────


def build_tracker_graph() -> StateGraph:
    """
    Tracker subgraph topology:

      load_pipeline → schedule_followup → flag_overdue → render_dashboard
                                                                │
                                                         update_status → END

    schedule_followup runs before flag_overdue so dates are written before
    the overdue check reads them. flag_overdue runs before render_dashboard
    so the dashboard has the warnings list ready. update_status is last and
    is a no-op unless tracker_new_status is set in state.

    LangGraph pattern: graph as workflow orchestrator for purely synchronous
    DB operations — no LLMs, no async I/O required.
    """
    graph = StateGraph(PipelineState)

    graph.add_node("load_pipeline", load_pipeline)
    graph.add_node("schedule_followup", schedule_followup)
    graph.add_node("flag_overdue", flag_overdue)
    graph.add_node("render_dashboard", render_dashboard)
    graph.add_node("update_status", update_status)

    graph.set_entry_point("load_pipeline")
    graph.add_edge("load_pipeline", "schedule_followup")
    graph.add_edge("schedule_followup", "flag_overdue")
    graph.add_edge("flag_overdue", "render_dashboard")
    graph.add_edge("render_dashboard", "update_status")
    graph.add_edge("update_status", END)

    return graph
