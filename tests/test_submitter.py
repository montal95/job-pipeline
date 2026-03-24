"""
Submitter agent tests — file input detection, ATS field mapping, conditional
document rendering, confirmation detection, and submission routing.

All tests use HTML fixtures and an in-memory SQLite DB.
No live browser, no Playwright.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
GREENHOUSE_FORM = FIXTURES / "greenhouse_form.html"
GREENHOUSE_NO_UPLOAD = FIXTURES / "greenhouse_form_no_upload.html"
WORKDAY_FORM = FIXTURES / "workday_form.html"
GREENHOUSE_CONFIRM = FIXTURES / "greenhouse_confirmation.html"
SUBMISSION_ERROR = FIXTURES / "submission_error.html"


@pytest.fixture()
def submitter_settings(tmp_path, seeded_db):
    """
    Patches pipeline.agents.submitter.settings with seeded_db and tmp_path.
    Use for render node tests that need content JSON present in the DB.
    Yields the mock settings object for inspection if needed.
    """
    with patch("pipeline.agents.submitter.settings") as mock_settings:
        mock_settings.app_db_path = seeded_db
        mock_settings.output_dir = str(tmp_path)
        yield mock_settings


# ── File input detection ──────────────────────────────────────────────────────


def test_has_file_input_detects_present():
    from pipeline.agents.submitter import _has_file_input
    assert _has_file_input(GREENHOUSE_FORM.read_text(encoding="utf-8")) is True


def test_has_file_input_detects_absent():
    from pipeline.agents.submitter import _has_file_input
    assert _has_file_input(GREENHOUSE_NO_UPLOAD.read_text(encoding="utf-8")) is False


def test_has_file_input_detects_hidden_input():
    """CSS-hidden file inputs are still accessible via Playwright setInputFiles."""
    from pipeline.agents.submitter import _has_file_input
    assert _has_file_input('<form><input type="file" style="display:none"></form>') is True


# ── ATS field mapping ─────────────────────────────────────────────────────────


def test_map_form_fields_greenhouse_happy_path():
    """label[for] → #id mapping; file inputs excluded from the field map."""
    from pipeline.agents.submitter import _map_form_fields
    from pipeline.state import AtsType
    result = _map_form_fields(GREENHOUSE_FORM.read_text(encoding="utf-8"), AtsType.GREENHOUSE)
    assert result.get("First Name") == "#first_name"
    assert result.get("Last Name") == "#last_name"
    assert result.get("Email Address") == "#email"
    for selector in result.values():
        assert "resume" not in selector.lower() or "linkedin" in selector.lower()


def test_map_form_fields_greenhouse_empty_form():
    from pipeline.agents.submitter import _map_form_fields
    from pipeline.state import AtsType
    assert _map_form_fields("<form></form>", AtsType.GREENHOUSE) == {}


def test_map_form_fields_workday_happy_path():
    """aria-label → [aria-label='X'] selector mapping for Workday forms."""
    from pipeline.agents.submitter import _map_form_fields
    from pipeline.state import AtsType
    result = _map_form_fields(WORKDAY_FORM.read_text(encoding="utf-8"), AtsType.WORKDAY)
    assert result.get("First Name") == "[aria-label='First Name']"
    assert result.get("Last Name") == "[aria-label='Last Name']"
    assert result.get("Email Address") == "[aria-label='Email Address']"


# ── Conditional render node ───────────────────────────────────────────────────


def test_render_documents_if_needed_renders_when_upload_required(
    sample_job, submitter_settings, tmp_path
):
    """needs_file_upload=True + content JSON in DB → .docx files created."""
    from pipeline.agents.submitter import _render_documents_if_needed
    from pipeline.state import empty_state

    state = empty_state()
    state["current_job_id"] = sample_job.id
    state["needs_file_upload"] = True

    result = _render_documents_if_needed(state)

    assert result.get("resume_path") is not None
    assert result.get("cover_letter_path") is not None
    assert Path(result["resume_path"]).exists()
    assert Path(result["cover_letter_path"]).exists()


def test_render_documents_if_needed_skips_when_no_upload(
    sample_job, submitter_settings, tmp_path
):
    """needs_file_upload=False → no-op, no files created."""
    from pipeline.agents.submitter import _render_documents_if_needed
    from pipeline.state import empty_state

    state = empty_state()
    state["current_job_id"] = sample_job.id
    state["needs_file_upload"] = False

    result = _render_documents_if_needed(state)

    assert result == {}
    assert list(tmp_path.glob("*.docx")) == []


def test_render_documents_if_needed_errors_on_missing_content(
    sample_job, bare_db, tmp_path
):
    """Content JSON absent from DB → errors list populated."""
    from pipeline.agents.submitter import _render_documents_if_needed
    from pipeline.state import empty_state

    state = empty_state()
    state["current_job_id"] = sample_job.id
    state["needs_file_upload"] = True

    with patch("pipeline.agents.submitter.settings") as mock_settings:
        mock_settings.app_db_path = bare_db
        mock_settings.output_dir = str(tmp_path)
        result = _render_documents_if_needed(state)

    assert len(result.get("errors", [])) > 0


# ── Confirmation detection ────────────────────────────────────────────────────


def test_parse_confirmation_detects_success():
    from pipeline.agents.submitter import _parse_confirmation
    assert _parse_confirmation(GREENHOUSE_CONFIRM.read_text(encoding="utf-8")) is True


def test_parse_confirmation_detects_error_page():
    from pipeline.agents.submitter import _parse_confirmation
    assert _parse_confirmation(SUBMISSION_ERROR.read_text(encoding="utf-8")) is False


# ── Submission routing ────────────────────────────────────────────────────────


def test_should_submit_routes_to_fill_form_when_ready():
    """No errors + ats_field_map populated → 'fill_form'."""
    from pipeline.agents.submitter import should_submit
    from pipeline.state import empty_state

    state = empty_state()
    state["ats_field_map"] = {"First Name": "#first_name", "Email Address": "#email"}
    state["errors"] = []
    assert should_submit(state) == "fill_form"


def test_should_submit_routes_to_abort_on_error():
    """Errors present → 'abort_submission' regardless of field map content."""
    from pipeline.agents.submitter import should_submit
    from pipeline.state import empty_state

    state = empty_state()
    state["ats_field_map"] = {"First Name": "#first_name"}
    state["errors"] = ["scan_form: Playwright navigation failed"]
    assert should_submit(state) == "abort_submission"
