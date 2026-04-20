"""
Shared pytest fixtures available to all test modules.

Fixtures defined here are auto-discovered by pytest — no import needed.
Any test file can request sample_job, resume_content, cover_letter_content,
seeded_db, bare_db, or mock_llm_message without duplicating setup code.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_JOB_JSON = FIXTURES / "sample_job.json"
SAMPLE_RESUME_LLM = FIXTURES / "sample_resume_llm_response.json"
SAMPLE_CL_LLM = FIXTURES / "sample_cover_letter_llm_response.json"


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
    Minimal SQLite DB with migrations 001+002 applied and one jobs row
    pre-inserted with resume_content_json and cover_letter_content_json populated.
    Returns the db path string.
    """
    db_path = str(tmp_path / "test_pipeline.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        (Path(__file__).parent.parent / "migrations" / "001_initial.sql").read_text()
    )
    try:
        conn.execute("ALTER TABLE jobs ADD COLUMN resume_content_json TEXT")
        conn.execute("ALTER TABLE jobs ADD COLUMN cover_letter_content_json TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # columns already exist from a prior partial run

    conn.execute(
        """
        INSERT INTO jobs (
            id, title, company, location, source, source_url,
            discovered_at, status, fingerprint,
            resume_content_json, cover_letter_content_json
        ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'), 'queued', '', ?, ?)
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


@pytest.fixture()
def bare_db(tmp_path, sample_job):
    """
    Minimal SQLite DB with migrations 001+002 applied and one jobs row inserted,
    but resume_content_json and cover_letter_content_json are NULL.

    Use this to test error paths where content JSON is expected but absent.
    Contrast with seeded_db which has content JSON populated.
    """
    db_path = str(tmp_path / "bare_pipeline.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        (Path(__file__).parent.parent / "migrations" / "001_initial.sql").read_text()
    )
    try:
        conn.execute("ALTER TABLE jobs ADD COLUMN resume_content_json TEXT")
        conn.execute("ALTER TABLE jobs ADD COLUMN cover_letter_content_json TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    conn.execute(
        "INSERT INTO jobs (id, title, company, location, source, source_url, "
        "discovered_at, status, fingerprint) VALUES (?, 'T', 'C', 'L', 's', 'u', "
        "datetime('now'), 'queued', '')",
        (sample_job.id,),
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture()
def mock_llm_message():
    """
    A pre-built MagicMock that mimics an Anthropic API response containing
    the sample resume LLM JSON fixture.

    Use this wherever a test needs to patch anthropic.Anthropic and provide
    a canned response from messages.create().
    """
    msg = MagicMock()
    msg.content = [MagicMock(text=(FIXTURES / "sample_resume_llm_response.json").read_text(encoding="utf-8"))]
    return msg


@pytest.fixture()
def mock_cl_llm_message():
    """MagicMock mimicking an Anthropic response containing the sample cover letter JSON fixture."""
    msg = MagicMock()
    msg.content = [MagicMock(text=(FIXTURES / "sample_cover_letter_llm_response.json").read_text(encoding="utf-8"))]
    return msg


@pytest.fixture()
def tracker_db(tmp_path):
    """
    SQLite DB with all migrations applied and a small set of jobs + submissions
    pre-inserted for Tracker agent tests.

    Jobs:
      tracker-job-001  status=applied   → has an overdue submission (sub-001)
      tracker-job-002  status=applied   → has a future-due submission (sub-002)
      tracker-job-003  status=rejected  → no submission; used to verify ignored by overdue logic

    The job IDs are stable strings (not UUIDs) so test assertions are readable.
    """
    from tests.fixtures.sample_submissions import (
        SAMPLE_SUBMISSIONS,
        TRACKER_JOB_ID_APPLIED,
        TRACKER_JOB_ID_APPLIED_FUTURE,
        TRACKER_JOB_ID_REJECTED,
    )

    db_path = str(tmp_path / "tracker_pipeline.db")
    conn = sqlite3.connect(db_path)

    migrations_dir = Path(__file__).parent.parent / "migrations"
    for migration_file in sorted(migrations_dir.glob("*.sql")):
        try:
            conn.executescript(migration_file.read_text())
        except sqlite3.OperationalError:
            pass  # duplicate column — already applied
    conn.commit()

    jobs = [
        (TRACKER_JOB_ID_APPLIED,        "Senior Rails Engineer", "Acme Health",   "Chicago, IL", "applied",  "fp_test_001"),
        (TRACKER_JOB_ID_APPLIED_FUTURE,  "Backend Engineer",     "Startup Inc",   "Remote",      "applied",  "fp_test_002"),
        (TRACKER_JOB_ID_REJECTED,        "AI Engineer",          "DataCo",        "New York, NY","rejected", "fp_test_003"),
    ]
    for job_id, title, company, location, status, fingerprint in jobs:
        conn.execute(
            "INSERT INTO jobs (id, title, company, location, source, source_url, "
            "discovered_at, status, fingerprint) VALUES (?, ?, ?, ?, 'indeed', 'http://x', "
            "datetime('now'), ?, ?)",
            (job_id, title, company, location, status, fingerprint),
        )

    for sub in SAMPLE_SUBMISSIONS:
        conn.execute(
            "INSERT INTO submissions (id, job_id, submitted_at, followup_due_date) "
            "VALUES (?, ?, ?, ?)",
            (sub["id"], sub["job_id"], sub["submitted_at"], sub["followup_due_date"]),
        )

    conn.commit()
    conn.close()
    return db_path
