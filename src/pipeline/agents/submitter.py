"""
Submitter Agent — Phase 4 implementation.

Responsibility: Fill and submit the ATS form for a specific job.
Zero LLM calls — all form filling is deterministic from ATS field maps.

LangGraph patterns exercised here:
  - interrupt() for the hard submission gate (no form touched without explicit yes)
  - Conditional render: docx files only created when ATS form has a file upload input
  - Swappable ATS strategy modules (Greenhouse, Workday, Ashby, etc.)
"""

from __future__ import annotations

from bs4 import BeautifulSoup
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from pipeline.config import settings
from pipeline.state import AtsType, CoverLetterContent, PipelineState, ResumeContent


# ── Pure helper functions (unit testable — no Playwright, no DB, no LLM) ──────


def _has_file_input(html: str) -> bool:
    """
    Return True if form HTML contains any <input type="file"> element,
    regardless of CSS visibility. Hidden file inputs are still accessible
    via Playwright's setInputFiles and must be included in detection.
    """
    soup = BeautifulSoup(html, "html.parser")
    return bool(soup.find("input", {"type": "file"}))


def _map_form_fields(html: str, ats_type: AtsType) -> dict[str, str]:
    """
    Extract field label → CSS selector mappings from ATS form HTML.

    Greenhouse strategy:
      Find <label for="X"> elements. Map label text → "#X" selector.
      Skip labels whose associated input has type="file" — file inputs
      are handled separately by _render_documents_if_needed.

    Workday strategy:
      Find inputs/selects with aria-label attributes.
      Map aria-label value → "[aria-label='X']" selector.
      No file inputs expected on Workday forms (uses separate resume parser UI).

    Returns empty dict for unrecognized ATS types or forms with no mappable fields.
    """
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, str] = {}

    if ats_type == AtsType.GREENHOUSE:
        for label in soup.find_all("label", attrs={"for": True}):
            field_id = label.get("for")
            if not field_id:
                continue
            # Find the associated input element
            target = soup.find(id=field_id)
            if target is None:
                continue
            # Skip file inputs — handled by render_documents_if_needed
            if target.get("type", "").lower() == "file":
                continue
            label_text = label.get_text(strip=True)
            if label_text:
                result[label_text] = f"#{field_id}"

    elif ats_type == AtsType.WORKDAY:
        for elem in soup.find_all(True, {"aria-label": True}):
            if elem.name not in ("input", "select", "textarea"):
                continue
            aria_label = elem.get("aria-label", "").strip()
            if aria_label:
                result[aria_label] = f"[aria-label='{aria_label}']"

    return result


def _parse_confirmation(html: str) -> bool:
    """
    Return True if post-submit page HTML contains success indicators.

    Checks (case-insensitive):
      'application submitted', 'thank you', 'we received your',
      'confirmation number'

    Returns False if no success signal is found.
    """
    text = html.lower()
    success_signals = [
        "application submitted",
        "thank you",
        "we received your",
        "confirmation number",
    ]
    return any(signal in text for signal in success_signals)


def _render_documents_if_needed(state: PipelineState) -> dict:
    """
    Read resume_content_json + cover_letter_content_json from DB.

    If needs_file_upload is True:
      Deserialize content models, call _render_resume_docx and
      _render_cover_letter_docx (imported from writer.py), return paths.
    If needs_file_upload is False:
      Return {} — no-op, no files created.
    If content JSON is missing from DB:
      Append to errors, return {}.
    """
    import sqlite3

    if not state.get("needs_file_upload"):
        return {}

    from pipeline.agents.writer import _render_cover_letter_docx, _render_resume_docx

    job_id = state.get("current_job_id")
    db_path = str(settings.app_db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT resume_content_json, cover_letter_content_json FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None or not row["resume_content_json"]:
        return {
            "errors": state.get("errors", [])
            + [f"_render_documents_if_needed: content JSON missing for job '{job_id}'"]
        }

    try:
        resume_content = ResumeContent.model_validate_json(row["resume_content_json"])
        cl_content = CoverLetterContent.model_validate_json(
            row["cover_letter_content_json"]
        )
    except Exception as exc:
        return {
            "errors": state.get("errors", [])
            + [f"_render_documents_if_needed: failed to deserialize content — {exc}"]
        }

    output_dir = str(settings.output_dir)
    resume_path = f"{output_dir}/{job_id}_resume.docx"
    cover_letter_path = f"{output_dir}/{job_id}_cover_letter.docx"

    _render_resume_docx(resume_content, resume_path)
    _render_cover_letter_docx(cl_content, cover_letter_path)

    return {"resume_path": resume_path, "cover_letter_path": cover_letter_path}


# ── Node stubs ─────────────────────────────────────────────────────────────────


def load_job(state: PipelineState) -> dict:
    """
    Fetch job record from DB by current_job_id and add to shortlist.
    Same pattern as writer.load_job — sync sqlite3 to avoid event loop conflicts.
    Skips if job is already in shortlist (supports resume-from-checkpoint).
    """
    import sqlite3

    job_id = state.get("current_job_id")
    if not job_id:
        return {"errors": state.get("errors", []) + ["load_job: current_job_id not set"]}

    if any(j.id == job_id for j in state.get("shortlist", [])):
        return {}

    from pipeline.state import JobListing

    conn = sqlite3.connect(str(settings.app_db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()

    if row is None:
        return {"errors": state.get("errors", []) + [f"load_job: job '{job_id}' not found in DB"]}

    job = JobListing.model_validate({
        "id": row["id"],
        "title": row["title"],
        "company": row["company"],
        "location": row["location"],
        "workplace_type": row["workplace_type"],
        "source": row["source"],
        "source_url": row["source_url"],
        "apply_url": row["apply_url"],
        "ats_type": row["ats_type"] or "unknown",
        "description": row["description"],
        "compensation_low": row["compensation_low"],
        "compensation_high": row["compensation_high"],
        "posted_date": row["posted_date"],
        "discovered_at": row["discovered_at"],
        "status": row["status"],
        "fit_signal": row["fit_signal"],
        "company_headcount": row["company_headcount"],
        "fingerprint": row["fingerprint"] or "",
        "resume_path": row["resume_path"],
        "cover_letter_path": row["cover_letter_path"],
        "notes": row["notes"],
    })
    return {"shortlist": state.get("shortlist", []) + [job]}


def detect_ats(state: PipelineState) -> dict:
    """
    Fingerprint the apply_url to determine ATS type.
    Imports detect_ats from pipeline.ats — shared with Discoverer.
    Writes ats_type back to state for downstream nodes.
    """
    from pipeline.ats import detect_ats as _detect

    job = next(
        (j for j in state.get("shortlist", []) if j.id == state.get("current_job_id")),
        None,
    )
    if job is None:
        return {}

    ats_type = _detect(job.apply_url)
    return {"shortlist": [
        j.model_copy(update={"ats_type": ats_type}) if j.id == job.id else j
        for j in state.get("shortlist", [])
    ]}


def scan_form(state: PipelineState) -> dict:
    """
    Navigate to apply_url with Playwright, snapshot the form HTML, detect file
    inputs and build the ATS field map.

    Sets:
      needs_file_upload: bool  — True if any <input type="file"> found
      ats_field_map: dict      — label → CSS selector for text/email/tel fields

    Safe fallback if Playwright is unavailable or navigation fails:
      needs_file_upload=True, ats_field_map={}, warning appended.
    This ensures the render node runs and files are available even if scan fails.

    Workday note: Workday drops connections on programmatic navigation.
    When Workday is detected, scan_form surfaces a warning and returns an empty
    field map — fill_form handles the Workday fallback path.
    """
    job = next(
        (j for j in state.get("shortlist", []) if j.id == state.get("current_job_id")),
        None,
    )
    if job is None or not job.apply_url:
        return {
            "needs_file_upload": True,
            "ats_field_map": {},
            "warnings": state.get("warnings", []) + ["scan_form: no apply_url — skipping form scan"],
        }

    # Workday: skip programmatic navigation, surface manual fallback warning
    if job.ats_type == AtsType.WORKDAY:
        return {
            "needs_file_upload": False,
            "ats_field_map": {},
            "warnings": state.get("warnings", []) + [
                f"scan_form: Workday detected — programmatic navigation blocked. "
                f"Open {job.apply_url} manually in Chrome, then resume."
            ],
        }

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(job.apply_url, wait_until="domcontentloaded", timeout=15000)
            html = page.content()
            browser.close()

        needs_upload = _has_file_input(html)
        field_map = _map_form_fields(html, job.ats_type)
        return {"needs_file_upload": needs_upload, "ats_field_map": field_map}

    except Exception as exc:
        return {
            "needs_file_upload": True,
            "ats_field_map": {},
            "warnings": state.get("warnings", []) + [f"scan_form: navigation failed ({exc}) — defaulting to upload=True"],
        }


def render_documents_if_needed(state: PipelineState) -> dict:
    """Node wrapper for _render_documents_if_needed. Phase 7 target."""
    return _render_documents_if_needed(state)


def fill_form(state: PipelineState) -> dict:
    """Playwright form fill using ats_field_map. Phase 8 target."""
    return {}


def submission_gate(state: PipelineState) -> dict:
    """Hard interrupt gate — no form submitted without explicit 'yes'. Phase 8 target."""
    interrupt({
        "message": "Review form summary — reply 'yes' to submit, anything else to abort.",
    })
    return {}


def submit_form(state: PipelineState) -> dict:
    """Click submit, wait for confirmation. Phase 9 target."""
    return {}


def capture_confirmation(state: PipelineState) -> dict:
    """Screenshot + text capture of confirmation state. Phase 9 target."""
    return {}


def persist_submission(state: PipelineState) -> dict:
    """Write to submissions table, update job status → applied. Phase 9 target."""
    return {}


def abort_submission(state: PipelineState) -> dict:
    """User declined or error encountered — log and exit cleanly."""
    return {
        "warnings": state.get("warnings", [])
        + ["Submission aborted."]
    }


def should_submit(state: PipelineState) -> str:
    """
    Conditional edge after scan_form.
    Routes to fill_form if no errors and ats_field_map is populated.
    Routes to abort_submission if errors were recorded during scanning.
    """
    if state.get("errors"):
        return "abort_submission"
    if not state.get("ats_field_map"):
        return "abort_submission"
    return "fill_form"


def should_submit_after_gate(state: PipelineState) -> str:
    """Conditional edge after submission_gate interrupt. Routes on human_approved."""
    if state.get("human_approved"):
        return "submit_form"
    return "abort_submission"


# ── Graph assembly ─────────────────────────────────────────────────────────────


def build_submitter_graph() -> StateGraph:
    """
    Submitter subgraph topology (from phase4-handoff.md):

    load_job → detect_ats → scan_form
                                │
                   should_submit (fill_form / abort_submission)
                                │
                    render_documents_if_needed
                                │
                           fill_form
                                │
                      submission_gate (interrupt)
                                │
             should_submit_after_gate (submit_form / abort_submission)
                                │
                         submit_form → capture_confirmation → persist_submission → END
                                │
                       abort_submission → END

    LangGraph pattern: hard interrupt at submission_gate — no form is ever
    submitted without explicit 'yes' from the user via Command(resume='yes').
    """
    graph = StateGraph(PipelineState)

    graph.add_node("load_job", load_job)
    graph.add_node("detect_ats", detect_ats)
    graph.add_node("scan_form", scan_form)
    graph.add_node("render_documents_if_needed", render_documents_if_needed)
    graph.add_node("fill_form", fill_form)
    graph.add_node("submission_gate", submission_gate)
    graph.add_node("submit_form", submit_form)
    graph.add_node("capture_confirmation", capture_confirmation)
    graph.add_node("persist_submission", persist_submission)
    graph.add_node("abort_submission", abort_submission)

    graph.set_entry_point("load_job")
    graph.add_edge("load_job", "detect_ats")
    graph.add_edge("detect_ats", "scan_form")

    graph.add_conditional_edges(
        "scan_form",
        should_submit,
        {
            "fill_form": "render_documents_if_needed",
            "abort_submission": "abort_submission",
        },
    )
    graph.add_edge("render_documents_if_needed", "fill_form")
    graph.add_edge("fill_form", "submission_gate")

    graph.add_conditional_edges(
        "submission_gate",
        should_submit_after_gate,
        {
            "submit_form": "submit_form",
            "abort_submission": "abort_submission",
        },
    )
    graph.add_edge("submit_form", "capture_confirmation")
    graph.add_edge("capture_confirmation", "persist_submission")
    graph.add_edge("persist_submission", END)
    graph.add_edge("abort_submission", END)

    return graph
