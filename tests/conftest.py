"""
Shared pytest fixtures available to all test modules.

Fixtures defined here are auto-discovered by pytest — no import needed.
Any test file can request sample_job, resume_content, cover_letter_content,
or seeded_db without duplicating the setup code.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

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
