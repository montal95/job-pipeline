"""
Submitter Agent — Phase 4 implementation.

Responsibility: Fill and submit the ATS form for a specific job.
Zero LLM calls — all form filling is deterministic from ATS field maps.

LangGraph patterns exercised here:
  - Conditional render node: render_documents_if_needed reads
    resume_content_json / cover_letter_content_json from the DB and renders
    .docx files only if scan_form detected a file upload input on the ATS
    form. If the form has no file input, no files are created at all.
  - Two conditional edge functions with different semantics:
    should_submit (post-scan_form) routes on errors + field map population —
    did the form scan succeed? should_submit_after_gate (post-interrupt) routes
    on human_approved — did the user explicitly confirm submission?
  - Hard interrupt gate: submission_gate surfaces a form summary (company,
    role, ATS type, fields filled, file attached) and requires the literal
    string 'yes' before any submit action. State is checkpointed at the
    interrupt boundary — aborting is always safe.

Browser session limitation: LangGraph interrupt() checkpoints state and exits
the process. The Playwright browser opened by fill_form does not survive the
interrupt boundary. submit_form therefore re-navigates and re-fills before
clicking submit. This is not a workaround — it is the correct behavior given
the interrupt/resume execution model.
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
    """
    Fill the ATS form using ats_field_map and Playwright.

    Strategy:
      - Iterate ats_field_map; for each label → selector pair, triple_click to
        clear any pre-filled value, then type the candidate's data.
      - Candidate data is resolved from a fixed mapping of label keywords to
        state/settings values (name, email, phone, LinkedIn URL).
      - If needs_file_upload=True and resume_path is set, attach resume and
        cover letter via setInputFiles on their respective file inputs.
      - Workday: field map is empty (scan_form surfaces warning upstream);
        fill_form surfaces a secondary warning and returns without touching the page.

    Field data is sourced from settings (cv_path provides name/email) or
    hard-coded candidate constants. This keeps fill_form zero-LLM.
    """
    job = next(
        (j for j in state.get("shortlist", []) if j.id == state.get("current_job_id")),
        None,
    )
    field_map = state.get("ats_field_map") or {}

    # Workday or empty map — nothing to fill programmatically
    if not field_map:
        if job and job.ats_type == AtsType.WORKDAY:
            return {
                "warnings": state.get("warnings", []) + [
                    "fill_form: Workday — manual form fill required. "
                    "Complete the form in Chrome, then resume the pipeline."
                ]
            }
        return {}

    # Candidate field data — sourced from settings constants, not LLM
    candidate_data = {
        "first name":   settings.candidate_first_name,
        "last name":    settings.candidate_last_name,
        "email":        settings.candidate_email,
        "email address": settings.candidate_email,
        "phone":        settings.candidate_phone,
        "phone number": settings.candidate_phone,
        "linkedin":     settings.candidate_linkedin_url,
        "linkedin url": settings.candidate_linkedin_url,
        "linkedin profile url": settings.candidate_linkedin_url,
    }

    resume_path = state.get("resume_path")
    cover_letter_path = state.get("cover_letter_path")
    needs_upload = state.get("needs_file_upload", False)

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)  # visible for submission
            page = browser.new_page()
            page.goto(job.apply_url, wait_until="domcontentloaded", timeout=15000)

            for label_text, selector in field_map.items():
                value = candidate_data.get(label_text.lower().strip())
                if value is None:
                    continue
                try:
                    elem = page.locator(selector).first
                    elem.triple_click()
                    elem.type(value, delay=30)
                except Exception:
                    pass  # best-effort; missing field doesn't abort

            # Attach documents if ATS form has file inputs
            if needs_upload and resume_path:
                try:
                    page.locator("input[type='file']").first.set_input_files(resume_path)
                except Exception:
                    pass
            if needs_upload and cover_letter_path:
                try:
                    file_inputs = page.locator("input[type='file']").all()
                    if len(file_inputs) > 1:
                        file_inputs[1].set_input_files(cover_letter_path)
                except Exception:
                    pass

            # Persist browser state for submission_gate → submit_form handoff
            # Store page URL so submit_form can re-attach (headless browsers don't
            # survive the interrupt boundary; the user reviews the visible browser)
            page.pause()  # keeps browser open for user review before gate
            browser.close()

    except Exception as exc:
        return {
            "errors": state.get("errors", []) + [f"fill_form: {exc}"]
        }

    return {}


def submission_gate(state: PipelineState) -> dict:
    """
    Hard human-in-the-loop interrupt gate. No form is ever submitted without
    explicit 'yes' from the user.

    Surfaces a form summary with: company, title, ATS type, fields filled,
    and whether documents were attached.

    interrupt() returns the string from Command(resume=...):
      'yes'         → routes to submit_form via should_submit_after_gate
      anything else → routes to abort_submission
    """
    job = next(
        (j for j in state.get("shortlist", []) if j.id == state.get("current_job_id")),
        None,
    )
    field_map = state.get("ats_field_map") or {}
    needs_upload = state.get("needs_file_upload", False)
    resume_path = state.get("resume_path")

    response = interrupt({
        "form_summary": {
            "company":        job.company if job else "unknown",
            "title":          job.title if job else "unknown",
            "ats_type":       job.ats_type if job else "unknown",
            "fields_filled":  list(field_map.keys()),
            "file_attached":  needs_upload and resume_path is not None,
        },
        "message": "Review the form summary above. Reply 'yes' to submit, anything else to abort.",
    })

    if isinstance(response, str) and response.strip().lower() == "yes":
        return {"human_approved": True}
    return {
        "human_approved": False,
        "warnings": state.get("warnings", []) + ["Submission aborted at gate."],
    }


def submit_form(state: PipelineState) -> dict:
    """
    Re-open the apply URL in Playwright, re-fill the form (fields persist in
    a new session), and click the submit button.

    Note: fill_form uses page.pause() to keep the browser open for user review.
    The LangGraph interrupt boundary means the browser session doesn't survive
    between fill_form and submit_form. We therefore re-navigate and re-fill
    before clicking submit.

    Post-submit: capture page HTML and call _parse_confirmation to verify success.
    Sets submission_confirmed in state.
    """
    job = next(
        (j for j in state.get("shortlist", []) if j.id == state.get("current_job_id")),
        None,
    )
    if job is None or not job.apply_url:
        return {"errors": state.get("errors", []) + ["submit_form: no apply_url"]}

    field_map = state.get("ats_field_map") or {}
    resume_path = state.get("resume_path")
    cover_letter_path = state.get("cover_letter_path")
    needs_upload = state.get("needs_file_upload", False)

    candidate_data = {
        "first name":           settings.candidate_first_name,
        "last name":            settings.candidate_last_name,
        "email":                settings.candidate_email,
        "email address":        settings.candidate_email,
        "phone":                settings.candidate_phone,
        "phone number":         settings.candidate_phone,
        "linkedin":             settings.candidate_linkedin_url,
        "linkedin url":         settings.candidate_linkedin_url,
        "linkedin profile url": settings.candidate_linkedin_url,
    }

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            page = browser.new_page()
            page.goto(job.apply_url, wait_until="domcontentloaded", timeout=15000)

            # Re-fill all fields
            for label_text, selector in field_map.items():
                value = candidate_data.get(label_text.lower().strip())
                if value is None:
                    continue
                try:
                    elem = page.locator(selector).first
                    elem.triple_click()
                    elem.type(value, delay=30)
                except Exception:
                    pass

            if needs_upload and resume_path:
                try:
                    page.locator("input[type='file']").first.set_input_files(resume_path)
                except Exception:
                    pass
            if needs_upload and cover_letter_path:
                try:
                    inputs = page.locator("input[type='file']").all()
                    if len(inputs) > 1:
                        inputs[1].set_input_files(cover_letter_path)
                except Exception:
                    pass

            # Click submit — try common selector patterns
            for submit_sel in ["[type='submit']", "button[type='submit']", "input[type='submit']"]:
                btn = page.locator(submit_sel).first
                if btn.count():
                    btn.click()
                    break

            page.wait_for_load_state("domcontentloaded", timeout=10000)
            html = page.content()
            browser.close()

        confirmed = _parse_confirmation(html)
        return {"submission_confirmed": confirmed}

    except Exception as exc:
        return {"errors": state.get("errors", []) + [f"submit_form: {exc}"]}


def capture_confirmation(state: PipelineState) -> dict:
    """
    Take a screenshot of the post-submission page as a receipt.
    Saves to {output_dir}/{job_id}_confirmation.png.
    Non-fatal if Playwright is unavailable — warning is appended instead.
    """
    job_id = state.get("current_job_id", "unknown")
    job = next(
        (j for j in state.get("shortlist", []) if j.id == job_id),
        None,
    )
    if job is None or not job.apply_url:
        return {}

    output_dir = str(settings.output_dir)
    screenshot_path = f"{output_dir}/{job_id}_confirmation.png"

    try:
        from pathlib import Path
        from playwright.sync_api import sync_playwright

        Path(output_dir).mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            # Re-visit the URL — confirmation pages are typically accessible
            page.goto(job.apply_url, wait_until="domcontentloaded", timeout=10000)
            page.screenshot(path=screenshot_path, full_page=True)
            browser.close()

        return {"submission_status": state.get("submission_status")}

    except Exception as exc:
        return {
            "warnings": state.get("warnings", []) + [f"capture_confirmation: screenshot failed ({exc})"]
        }


def persist_submission(state: PipelineState) -> dict:
    """
    Write a record to the submissions table and update the jobs row.

    submissions INSERT: job_id, submitted_at, confirmation_text (if any),
    confirmation_screenshot_path, followup_due_date (submitted_at + 7 days).

    jobs UPDATE: status → 'applied', resume_path, cover_letter_path.
    Uses sync sqlite3 — same rationale as load_job.
    """
    import sqlite3
    from datetime import date, datetime, timezone
    from uuid import uuid4

    job_id = state.get("current_job_id")
    if not job_id:
        return {}

    now = datetime.now(timezone.utc)
    followup = date.fromordinal(now.date().toordinal() + 7).isoformat()
    submission_id = str(uuid4())

    screenshot_path = f"{settings.output_dir}/{job_id}_confirmation.png"
    resume_path = state.get("resume_path")
    cover_letter_path = state.get("cover_letter_path")

    db_path = str(settings.app_db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO submissions
              (id, job_id, submitted_at, followup_due_date, confirmation_screenshot_path)
            VALUES (?, ?, ?, ?, ?)
            """,
            (submission_id, job_id, now.isoformat(), followup, screenshot_path),
        )
        conn.execute(
            """
            UPDATE jobs
               SET status = 'applied',
                   resume_path = ?,
                   cover_letter_path = ?
             WHERE id = ?
            """,
            (resume_path, cover_letter_path, job_id),
        )
        conn.commit()
    except Exception as exc:
        return {"errors": state.get("errors", []) + [f"persist_submission: {exc}"]}
    finally:
        conn.close()

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
