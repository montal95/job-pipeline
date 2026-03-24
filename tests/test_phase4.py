"""
Phase 4 — Submitter agent tests.

All tests are written red-first per the Phase 4 handoff doc.
They go green across Commits 5–6.

Test groups:
  Writer regressions       (2)  — verify deferred-render contract
  File input detection     (3)  — _has_file_input
  ATS field mapping        (3)  — _map_form_fields
  Conditional render node  (3)  — _render_documents_if_needed
  Confirmation detection   (2)  — _parse_confirmation
  Submission routing       (2)  — should_submit
                          ---
  Total                   15
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Fixtures path ──────────────────────────────────────────────────────────────

FIXTURES = Path(__file__).parent / "fixtures"
GREENHOUSE_FORM = FIXTURES / "greenhouse_form.html"
GREENHOUSE_NO_UPLOAD = FIXTURES / "greenhouse_form_no_upload.html"
WORKDAY_FORM = FIXTURES / "workday_form.html"
GREENHOUSE_CONFIRM = FIXTURES / "greenhouse_confirmation.html"
SUBMISSION_ERROR = FIXTURES / "submission_error.html"
SAMPLE_JOB_JSON = FIXTURES / "sample_job.json"
SAMPLE_RESUME_LLM = FIXTURES / "sample_resume_llm_response.json"
SAMPLE_CL_LLM = FIXTURES / "sample_cover_letter_llm_response.json"


# ── Shared fixtures ────────────────────────────────────────────────────────────


@pytest.fixture()
def sample_job():
    from pipeline.state import JobListing
    return JobListing.model_validate_json(SAMPLE_JOB_JSON.read_text(encoding="utf-8"))


@pytest.fixture()
def resume_content():
    from pipeline.agents.writer import _parse_resume_json
    return _parse_resume_json(SAMPLE_RESUME_LLM.read_text(encoding="utf-8"))


@pytest.fixture()
def cover_letter_content():
    from pipeline.agents.writer import _parse_cover_letter_json
    return _parse_cover_letter_json(SAMPLE_CL_LLM.read_text(encoding="utf-8"))


@pytest.fixture()
def seeded_db(tmp_path, sample_job, resume_content, cover_letter_content):
    """
    Minimal SQLite DB with migrations 001+002 applied and one jobs row pre-inserted.
    resume_content_json and cover_letter_content_json are populated.
    Returns the db path string.
    """
    db_path = str(tmp_path / "test_pipeline.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        (Path(__file__).parent.parent / "migrations" / "001_initial.sql").read_text()
    )
    # Apply 002 manually (ALTER TABLE idempotency handled in tests)
    try:
        conn.execute("ALTER TABLE jobs ADD COLUMN resume_content_json TEXT")
        conn.execute("ALTER TABLE jobs ADD COLUMN cover_letter_content_json TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # columns already exist

    conn.execute(
        """
        INSERT INTO jobs (
            id, title, company, location, source, source_url,
            discovered_at, status, fingerprint,
            resume_content_json, cover_letter_content_json
        ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'), 'queued', '',  ?, ?)
        """,
        (
            sample_job.id,
            sample_job.title,
            sample_job.company,
            sample_job.location,
            sample_job.source,
            sample_job.source_url,
            resume_content.model_dump_json(),
            cover_letter_content.model_dump_json(),
        ),
    )
    conn.commit()
    conn.close()
    return db_path


# ── Writer regressions (2) ────────────────────────────────────────────────────


def test_write_resume_returns_content_not_path(tmp_path):
    """write_resume returns resume_content and no resume_path (deferred render)."""
    from pipeline.agents.writer import write_resume
    from pipeline.state import empty_state

    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=SAMPLE_RESUME_LLM.read_text(encoding="utf-8"))]

    state = empty_state()
    state["cv_text"] = "Samuel Montalvo — Backend Engineer"
    state["interview_answers"] = {}

    sample_job_raw = SAMPLE_JOB_JSON.read_text(encoding="utf-8")
    from pipeline.state import JobListing
    job = JobListing.model_validate_json(sample_job_raw)
    state["shortlist"] = [job]
    state["current_job_id"] = job.id

    with patch("pipeline.agents.writer.anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = mock_message
        result = write_resume(state)

    assert result.get("resume_content") is not None
    assert result.get("resume_path") is None


def test_persist_documents_saves_content_json(tmp_path, seeded_db, sample_job,
                                              resume_content, cover_letter_content):
    """persist_documents writes content JSON to DB; resume_path stays NULL."""
    from pipeline.agents.writer import persist_documents
    from pipeline.state import empty_state

    state = empty_state()
    state["current_job_id"] = sample_job.id
    state["resume_content"] = resume_content
    state["cover_letter_content"] = cover_letter_content

    with patch("pipeline.agents.writer.settings") as mock_settings:
        mock_settings.app_db_path = seeded_db
        persist_documents(state)

    conn = sqlite3.connect(seeded_db)
    row = conn.execute(
        "SELECT resume_path, resume_content_json FROM jobs WHERE id = ?",
        (sample_job.id,),
    ).fetchone()
    conn.close()

    assert row[0] is None, "resume_path should remain NULL (deferred render)"
    assert row[1] is not None, "resume_content_json should be populated"
    parsed = json.loads(row[1])
    assert parsed["name"] == resume_content.name



# ── File input detection (3) ──────────────────────────────────────────────────


def test_has_file_input_detects_present():
    """greenhouse_form.html contains file inputs → True."""
    from pipeline.agents.submitter import _has_file_input
    html = GREENHOUSE_FORM.read_text(encoding="utf-8")
    assert _has_file_input(html) is True


def test_has_file_input_detects_absent():
    """greenhouse_form_no_upload.html has no file inputs → False."""
    from pipeline.agents.submitter import _has_file_input
    html = GREENHOUSE_NO_UPLOAD.read_text(encoding="utf-8")
    assert _has_file_input(html) is False


def test_has_file_input_detects_hidden_input():
    """A hidden file input (display:none) is still a file input → True."""
    from pipeline.agents.submitter import _has_file_input
    html = '<form><input type="file" id="hidden_resume" style="display:none"></form>'
    assert _has_file_input(html) is True


# ── ATS field mapping (3) ─────────────────────────────────────────────────────


def test_map_form_fields_greenhouse_happy_path():
    """Greenhouse form: label-for → #id mapping, file inputs excluded."""
    from pipeline.agents.submitter import _map_form_fields
    from pipeline.state import AtsType
    html = GREENHOUSE_FORM.read_text(encoding="utf-8")
    result = _map_form_fields(html, AtsType.GREENHOUSE)
    # Text/email/tel inputs should be mapped; file inputs excluded
    assert result.get("First Name") == "#first_name"
    assert result.get("Last Name") == "#last_name"
    assert result.get("Email Address") == "#email"
    # File inputs must not appear in the field map
    for selector in result.values():
        assert "resume" not in selector.lower() or "linkedin" in selector.lower()


def test_map_form_fields_greenhouse_empty_form():
    """Empty form HTML returns an empty field map."""
    from pipeline.agents.submitter import _map_form_fields
    from pipeline.state import AtsType
    assert _map_form_fields("<form></form>", AtsType.GREENHOUSE) == {}


def test_map_form_fields_workday_happy_path():
    """Workday form: aria-label → [aria-label='X'] selector mapping."""
    from pipeline.agents.submitter import _map_form_fields
    from pipeline.state import AtsType
    html = WORKDAY_FORM.read_text(encoding="utf-8")
    result = _map_form_fields(html, AtsType.WORKDAY)
    assert result.get("First Name") == "[aria-label='First Name']"
    assert result.get("Last Name") == "[aria-label='Last Name']"
    assert result.get("Email Address") == "[aria-label='Email Address']"


# ── Conditional render node (3) ───────────────────────────────────────────────


def test_render_documents_if_needed_renders_when_upload_required(
    tmp_path, seeded_db, sample_job
):
    """When needs_file_upload=True and content JSON exists, .docx files are created."""
    from pipeline.agents.submitter import _render_documents_if_needed
    from pipeline.state import empty_state

    state = empty_state()
    state["current_job_id"] = sample_job.id
    state["needs_file_upload"] = True

    with patch("pipeline.agents.submitter.settings") as mock_settings:
        mock_settings.app_db_path = seeded_db
        mock_settings.output_dir = str(tmp_path)
        result = _render_documents_if_needed(state)

    assert result.get("resume_path") is not None
    assert result.get("cover_letter_path") is not None
    assert Path(result["resume_path"]).exists()
    assert Path(result["cover_letter_path"]).exists()


def test_render_documents_if_needed_skips_when_no_upload(
    tmp_path, seeded_db, sample_job
):
    """When needs_file_upload=False, the node is a no-op — no files created."""
    from pipeline.agents.submitter import _render_documents_if_needed
    from pipeline.state import empty_state

    state = empty_state()
    state["current_job_id"] = sample_job.id
    state["needs_file_upload"] = False

    with patch("pipeline.agents.submitter.settings") as mock_settings:
        mock_settings.app_db_path = seeded_db
        mock_settings.output_dir = str(tmp_path)
        result = _render_documents_if_needed(state)

    assert result == {}
    assert list(tmp_path.glob("*.docx")) == []


def test_render_documents_if_needed_errors_on_missing_content(
    tmp_path, sample_job
):
    """When content JSON is absent from DB, errors list is populated."""
    import sqlite3 as _sqlite3
    from pipeline.agents.submitter import _render_documents_if_needed
    from pipeline.state import empty_state

    # DB with migration applied but no content JSON (NULL columns)
    empty_db = str(tmp_path / "empty.db")
    conn = _sqlite3.connect(empty_db)
    conn.executescript(
        (Path(__file__).parent.parent / "migrations" / "001_initial.sql").read_text()
    )
    try:
        conn.execute("ALTER TABLE jobs ADD COLUMN resume_content_json TEXT")
        conn.execute("ALTER TABLE jobs ADD COLUMN cover_letter_content_json TEXT")
        conn.commit()
    except _sqlite3.OperationalError:
        pass
    conn.execute(
        "INSERT INTO jobs (id, title, company, location, source, source_url, "
        "discovered_at, status, fingerprint) VALUES (?, 'T', 'C', 'L', 's', 'u', "
        "datetime('now'), 'queued', '')",
        (sample_job.id,),
    )
    conn.commit()
    conn.close()

    state = empty_state()
    state["current_job_id"] = sample_job.id
    state["needs_file_upload"] = True

    with patch("pipeline.agents.submitter.settings") as mock_settings:
        mock_settings.app_db_path = empty_db
        mock_settings.output_dir = str(tmp_path)
        result = _render_documents_if_needed(state)

    assert len(result.get("errors", [])) > 0


# ── Confirmation detection (2) ────────────────────────────────────────────────


def test_parse_confirmation_detects_success():
    """greenhouse_confirmation.html contains success text → True."""
    from pipeline.agents.submitter import _parse_confirmation
    html = GREENHOUSE_CONFIRM.read_text(encoding="utf-8")
    assert _parse_confirmation(html) is True


def test_parse_confirmation_detects_error_page():
    """submission_error.html has no success signals → False."""
    from pipeline.agents.submitter import _parse_confirmation
    html = SUBMISSION_ERROR.read_text(encoding="utf-8")
    assert _parse_confirmation(html) is False


# ── Submission routing (2) ────────────────────────────────────────────────────


def test_should_submit_routes_to_fill_form_when_ready():
    """No errors + ats_field_map populated → 'fill_form'."""
    from pipeline.agents.submitter import should_submit
    from pipeline.state import empty_state

    state = empty_state()
    state["ats_field_map"] = {"First Name": "#first_name", "Email Address": "#email"}
    state["errors"] = []
    assert should_submit(state) == "fill_form"


def test_should_submit_routes_to_abort_on_error():
    """Errors present → 'abort_submission' regardless of field map."""
    from pipeline.agents.submitter import should_submit
    from pipeline.state import empty_state

    state = empty_state()
    state["ats_field_map"] = {"First Name": "#first_name"}
    state["errors"] = ["scan_form: Playwright navigation failed"]
    assert should_submit(state) == "abort_submission"
