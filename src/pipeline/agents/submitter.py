"""
Submitter Agent — Phase 4 implementation target.

Responsibility: Fill and submit the ATS form for a specific job.
Zero LLM calls — all form filling is deterministic from ATS field maps.

LangGraph patterns exercised here:
  - interrupt() for the hard submission gate (no form touched without explicit yes)
  - Swappable ATS strategy modules (Greenhouse, Workday, Ashby, etc.)

Current state: Phase 0 stubs.
"""

from __future__ import annotations

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from pipeline.state import AtsType, PipelineState


# ── Node stubs ─────────────────────────────────────────────────────────────────


def load_job(state: PipelineState) -> dict:
    """Fetch job record + doc paths from DB by current_job_id. Phase 4 target."""
    return {}


def detect_ats(state: PipelineState) -> dict:
    """
    Fingerprint the apply URL to determine ATS type.
    URL pattern matching — zero LLM calls. Phase 4 target.

    Known patterns:
      - boards.greenhouse.io / app.greenhouse.io → GREENHOUSE
      - *.workday.com                            → WORKDAY
      - jobs.ashbyhq.com                         → ASHBY
      - linkedin.com/jobs/easy-apply             → LINKEDIN
    """
    return {}


def load_ats_strategy(state: PipelineState) -> dict:
    """
    Load the ATS-specific field map and interaction patterns.
    ATS strategies are swappable modules — adding a new ATS doesn't touch existing ones.
    Phase 4 target.
    """
    return {}


def fill_form(state: PipelineState) -> dict:
    """
    Playwright-based form filling using the loaded ATS field map.
    Phase 4 target.

    ATS-specific notes:
      - Workday: attach to existing Chrome page (bot detection blocks direct nav)
      - Greenhouse: query by label text, not field ID (more stable)
      - LinkedIn Easy Apply: straightforward DOM, but multi-step flow
    """
    return {}


def submission_gate(state: PipelineState) -> dict:
    """
    Hard human-in-the-loop gate. Show form summary.
    Require explicit 'yes' before any submit action.
    No form is ever submitted without this confirmation.
    Phase 4 target.
    """
    interrupt(
        "Review form summary — confirm yes / edit / abort before submission"
    )
    return {}


def submit_form(state: PipelineState) -> dict:
    """Click submit, wait for confirmation page. Phase 4 target."""
    return {}


def capture_confirmation(state: PipelineState) -> dict:
    """Screenshot + text capture of confirmation state. Phase 4 target."""
    return {}


def persist_submission(state: PipelineState) -> dict:
    """
    Write to submissions table.
    Update job status → applied.
    Calculate followup_due_date.
    Phase 4 target.
    """
    return {}


def should_submit(state: PipelineState) -> str:
    """Conditional: route to submit if approved, else abort."""
    if state.get("human_approved"):
        return "submit_form"
    return "abort"


def abort(state: PipelineState) -> dict:
    """User chose not to submit. Log and exit cleanly."""
    return {
        "warnings": state.get("warnings", [])
        + ["Submission aborted at user request."]
    }


# ── Graph assembly ─────────────────────────────────────────────────────────────


def build_submitter_graph() -> StateGraph:
    graph = StateGraph(PipelineState)

    graph.add_node("load_job", load_job)
    graph.add_node("detect_ats", detect_ats)
    graph.add_node("load_ats_strategy", load_ats_strategy)
    graph.add_node("fill_form", fill_form)
    graph.add_node("submission_gate", submission_gate)
    graph.add_node("submit_form", submit_form)
    graph.add_node("capture_confirmation", capture_confirmation)
    graph.add_node("persist_submission", persist_submission)
    graph.add_node("abort", abort)

    graph.set_entry_point("load_job")
    graph.add_edge("load_job", "detect_ats")
    graph.add_edge("detect_ats", "load_ats_strategy")
    graph.add_edge("load_ats_strategy", "fill_form")
    graph.add_edge("fill_form", "submission_gate")

    graph.add_conditional_edges(
        "submission_gate",
        should_submit,
        {
            "submit_form": "submit_form",
            "abort": "abort",
        },
    )
    graph.add_edge("submit_form", "capture_confirmation")
    graph.add_edge("capture_confirmation", "persist_submission")
    graph.add_edge("persist_submission", END)
    graph.add_edge("abort", END)

    return graph
