"""
Tracker Agent — Phase 5 implementation target.

Responsibility: Status dashboard, follow-up scheduling, status updates.
Zero LLM calls — pure DB reads/writes and terminal rendering via rich.

LangGraph patterns exercised here:
  - Querying checkpoint state vs. application DB state (the distinction matters)

Current state: Phase 0 stubs.
"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from pipeline.state import PipelineState


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
