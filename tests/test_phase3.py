"""
Phase 3 — Writer agent tests.

All tests are written red-first: the pure functions and Pydantic models they
reference don't exist yet. They go green across Commits 4–6.

Test groups:
  CV loading          (2)
  Gap extraction      (3)
  Prompt builders     (2)
  JSON parsers        (4)
  docx rendering      (4)
  Node behavior       (3)
                     ---
  Total              18
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

# ── Fixtures path ──────────────────────────────────────────────────────────────

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_CV = FIXTURES / "sample_cv.txt"
SAMPLE_JOB_JSON = FIXTURES / "sample_job.json"
SAMPLE_RESUME_LLM = FIXTURES / "sample_resume_llm_response.json"
SAMPLE_CL_LLM = FIXTURES / "sample_cover_letter_llm_response.json"


# ── Shared fixture data ────────────────────────────────────────────────────────

@pytest.fixture()
def cv_text() -> str:
    return SAMPLE_CV.read_text(encoding="utf-8")


@pytest.fixture()
def sample_job():
    from pipeline.state import JobListing
    return JobListing.model_validate_json(SAMPLE_JOB_JSON.read_text(encoding="utf-8"))


@pytest.fixture()
def resume_llm_raw() -> str:
    return SAMPLE_RESUME_LLM.read_text(encoding="utf-8")


@pytest.fixture()
def cover_letter_llm_raw() -> str:
    return SAMPLE_CL_LLM.read_text(encoding="utf-8")


# ── CV loading ─────────────────────────────────────────────────────────────────

def test_load_cv_text_reads_file():
    """_load_cv_text returns the full string content of a text file."""
    from pipeline.agents.writer import _load_cv_text

    result = _load_cv_text(str(SAMPLE_CV))
    assert isinstance(result, str)
    assert len(result) > 100
    assert "Samuel Montalvo" in result


def test_load_cv_text_missing_file_raises():
    """_load_cv_text raises FileNotFoundError for a nonexistent path."""
    from pipeline.agents.writer import _load_cv_text

    with pytest.raises(FileNotFoundError):
        _load_cv_text("/nonexistent/path/cv.txt")


# ── Gap extraction ─────────────────────────────────────────────────────────────

def test_extract_job_gaps_finds_missing_skills(cv_text):
    """Skills in the JD that are absent from the CV surface as gaps."""
    from pipeline.agents.writer import _extract_job_gaps

    jd = "Experience with Kubernetes and Terraform required."
    gaps = _extract_job_gaps(cv_text, jd)

    assert any("kubernetes" in g.lower() for g in gaps)
    assert any("terraform" in g.lower() for g in gaps)


def test_extract_job_gaps_no_gaps(cv_text):
    """No gaps returned when all JD keywords are present in the CV."""
    from pipeline.agents.writer import _extract_job_gaps

    # Rails and PostgreSQL are both in the sample CV
    jd = "Experience with Rails and PostgreSQL required."
    gaps = _extract_job_gaps(cv_text, jd)

    assert gaps == []


def test_extract_job_gaps_case_insensitive(cv_text):
    """Gap detection is case-insensitive — 'kubernetes' != false gap for 'Kubernetes'."""
    from pipeline.agents.writer import _extract_job_gaps

    # CV has 'PostgreSQL'; JD uses 'postgresql' lowercase — should not be a gap
    jd = "Strong postgresql skills required."
    gaps = _extract_job_gaps(cv_text, jd)

    assert not any("postgresql" in g.lower() for g in gaps)


# ── Prompt builders ────────────────────────────────────────────────────────────

def test_build_resume_prompt_contains_job_title(cv_text, sample_job):
    """Resume prompt includes the target job title."""
    from pipeline.agents.writer import _build_resume_prompt

    prompt = _build_resume_prompt(cv_text, sample_job, answers={})
    assert "Senior Software Engineer" in prompt


def test_build_cover_letter_prompt_contains_company(cv_text, sample_job):
    """Cover letter prompt includes the target company name."""
    from pipeline.agents.writer import _build_cover_letter_prompt

    prompt = _build_cover_letter_prompt(cv_text, sample_job, answers={})
    assert "Acme Health" in prompt


# ── JSON parsers ───────────────────────────────────────────────────────────────

def test_parse_resume_json_happy_path(resume_llm_raw):
    """Valid fixture JSON parses into a ResumeContent model without errors."""
    from pipeline.agents.writer import _parse_resume_json
    from pipeline.state import ResumeContent

    result = _parse_resume_json(resume_llm_raw)
    assert isinstance(result, ResumeContent)
    assert result.name == "Samuel Montalvo"
    assert len(result.sections) > 0
    assert len(result.skills) > 0


def test_parse_resume_json_missing_field_raises():
    """Malformed JSON (missing required field) raises ValidationError."""
    from pipeline.agents.writer import _parse_resume_json

    bad_json = json.dumps({"name": "Test"})  # missing contact, summary, sections, skills
    with pytest.raises(ValidationError):
        _parse_resume_json(bad_json)


def test_parse_cover_letter_json_happy_path(cover_letter_llm_raw):
    """Valid fixture JSON parses into a CoverLetterContent model without errors."""
    from pipeline.agents.writer import _parse_cover_letter_json
    from pipeline.state import CoverLetterContent

    result = _parse_cover_letter_json(cover_letter_llm_raw)
    assert isinstance(result, CoverLetterContent)
    assert len(result.opening) > 0
    assert len(result.body_paragraphs) > 0
    assert len(result.closing) > 0


def test_parse_cover_letter_json_missing_field_raises():
    """Malformed JSON (missing required field) raises ValidationError."""
    from pipeline.agents.writer import _parse_cover_letter_json

    bad_json = json.dumps({"opening": "Hello"})  # missing body_paragraphs and closing
    with pytest.raises(ValidationError):
        _parse_cover_letter_json(bad_json)


# ── docx rendering ─────────────────────────────────────────────────────────────

@pytest.fixture()
def resume_content(resume_llm_raw):
    from pipeline.agents.writer import _parse_resume_json
    return _parse_resume_json(resume_llm_raw)


@pytest.fixture()
def cover_letter_content(cover_letter_llm_raw):
    from pipeline.agents.writer import _parse_cover_letter_json
    return _parse_cover_letter_json(cover_letter_llm_raw)


def test_render_resume_docx_creates_file(resume_content, tmp_path):
    """_render_resume_docx writes a .docx file at the given path."""
    from pipeline.agents.writer import _render_resume_docx

    out = str(tmp_path / "test_resume.docx")
    result_path = _render_resume_docx(resume_content, out)

    assert Path(result_path).exists()
    assert result_path.endswith(".docx")


def test_render_resume_docx_contains_name(resume_content, tmp_path):
    """Rendered resume docx contains the candidate's name in its text."""
    from docx import Document
    from pipeline.agents.writer import _render_resume_docx

    out = str(tmp_path / "test_resume.docx")
    _render_resume_docx(resume_content, out)

    doc = Document(out)
    full_text = "\n".join(p.text for p in doc.paragraphs)
    assert "Samuel Montalvo" in full_text


def test_render_cover_letter_docx_creates_file(cover_letter_content, tmp_path):
    """_render_cover_letter_docx writes a .docx file at the given path."""
    from pipeline.agents.writer import _render_cover_letter_docx

    out = str(tmp_path / "test_cover_letter.docx")
    result_path = _render_cover_letter_docx(cover_letter_content, out)

    assert Path(result_path).exists()
    assert result_path.endswith(".docx")


def test_render_cover_letter_docx_contains_opening(cover_letter_content, tmp_path):
    """Rendered cover letter docx contains text from the opening paragraph."""
    from docx import Document
    from pipeline.agents.writer import _render_cover_letter_docx

    out = str(tmp_path / "test_cover_letter.docx")
    _render_cover_letter_docx(cover_letter_content, out)

    doc = Document(out)
    full_text = "\n".join(p.text for p in doc.paragraphs)
    # Opening starts with "Dear Acme Health" per the fixture
    assert "Dear Acme Health" in full_text


# ── Node behavior ──────────────────────────────────────────────────────────────

def test_write_resume_calls_llm_once(cv_text, sample_job, tmp_path):
    """write_resume makes exactly one Anthropic API call and returns a resume_path."""
    from pipeline.agents.writer import write_resume
    from pipeline.state import empty_state

    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=SAMPLE_RESUME_LLM.read_text(encoding="utf-8"))]

    state = empty_state()
    state["cv_text"] = cv_text
    state["shortlist"] = [sample_job]
    state["current_job_id"] = sample_job.id
    state["interview_answers"] = {}

    with patch("pipeline.agents.writer.anthropic.Anthropic") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.messages.create.return_value = mock_message

        with patch("pipeline.agents.writer.settings") as mock_settings:
            mock_settings.output_dir = str(tmp_path)
            mock_settings.max_revision_rounds = 3
            result = write_resume(state)

    mock_client.messages.create.assert_called_once()
    assert result.get("resume_content") is not None


def test_apply_feedback_increments_revision_round(cv_text, sample_job, tmp_path):
    """apply_feedback increments revision_round by 1."""
    from pipeline.agents.writer import apply_feedback
    from pipeline.state import empty_state

    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=SAMPLE_RESUME_LLM.read_text(encoding="utf-8"))]

    state = empty_state()
    state["cv_text"] = cv_text
    state["shortlist"] = [sample_job]
    state["current_job_id"] = sample_job.id
    state["revision_round"] = 0
    state["human_feedback"] = "Please make the summary more concise."
    state["interview_answers"] = {}

    with patch("pipeline.agents.writer.anthropic.Anthropic") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.messages.create.return_value = mock_message

        with patch("pipeline.agents.writer.settings") as mock_settings:
            mock_settings.output_dir = str(tmp_path)
            mock_settings.max_revision_rounds = 3
            result = apply_feedback(state)

    assert result["revision_round"] == 1


def test_should_revise_routes_correctly():
    """should_revise returns the correct branch for all three routing cases."""
    from pipeline.agents.writer import should_revise
    from pipeline.state import empty_state

    with patch("pipeline.agents.writer.settings") as mock_settings:
        mock_settings.max_revision_rounds = 3

        # Case 1: feedback present, rounds remaining → apply_feedback
        s = empty_state()
        s["human_feedback"] = "Make it shorter."
        s["revision_round"] = 1
        assert should_revise(s) == "apply_feedback"

        # Case 2: no feedback → persist_documents
        s2 = empty_state()
        s2["human_feedback"] = None
        s2["revision_round"] = 0
        assert should_revise(s2) == "persist_documents"

        # Case 3: max rounds exceeded → warn_and_exit
        s3 = empty_state()
        s3["human_feedback"] = "Still not right."
        s3["revision_round"] = 3
        assert should_revise(s3) == "warn_and_exit"
