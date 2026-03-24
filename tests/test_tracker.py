"""
Tracker agent tests — status counting, overdue detection, days-since formatting,
load_pipeline node, and update_status node.

All tests use the tracker_db fixture or pure in-memory data.
No Rich terminal output tested (same rationale as Playwright nodes).
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from tests.fixtures.sample_submissions import (
    SAMPLE_SUBMISSIONS,
    TRACKER_JOB_ID_APPLIED,
    TRACKER_JOB_ID_APPLIED_FUTURE,
    TRACKER_JOB_ID_REJECTED,
)


# ── Status counting ───────────────────────────────────────────────────────────


def test_count_by_status_basic():
    """Returns correct count per status for a mixed list of jobs."""
    from pipeline.agents.tracker import _count_by_status
    from pipeline.state import JobListing, JobStatus

    def _job(status):
        return JobListing(
            source="indeed", title="T", company="C", location="L",
            source_url="http://x", status=status,
        )

    jobs = [
        _job(JobStatus.APPLIED),
        _job(JobStatus.APPLIED),
        _job(JobStatus.QUEUED),
        _job(JobStatus.REJECTED),
    ]
    counts = _count_by_status(jobs)
    assert counts["applied"] == 2
    assert counts["queued"] == 1
    assert counts["rejected"] == 1
    assert counts.get("new", 0) == 0


def test_count_by_status_empty():
    """Empty list returns empty dict."""
    from pipeline.agents.tracker import _count_by_status
    assert _count_by_status([]) == {}


# ── Overdue detection ─────────────────────────────────────────────────────────


def test_find_overdue_returns_past_due_jobs():
    """sub-001 has a past followup_due_date and status=applied → job ID returned."""
    from pipeline.agents.tracker import _find_overdue
    from pipeline.state import JobListing, JobStatus

    applied_job = JobListing(
        id=TRACKER_JOB_ID_APPLIED,
        source="indeed", title="T", company="C", location="L",
        source_url="http://x", status=JobStatus.APPLIED,
    )
    result = _find_overdue([applied_job], SAMPLE_SUBMISSIONS)
    assert TRACKER_JOB_ID_APPLIED in result


def test_find_overdue_ignores_future_due_date():
    """sub-002 has a far-future followup_due_date → job ID not returned."""
    from pipeline.agents.tracker import _find_overdue
    from pipeline.state import JobListing, JobStatus

    applied_job = JobListing(
        id=TRACKER_JOB_ID_APPLIED_FUTURE,
        source="indeed", title="T", company="C", location="L",
        source_url="http://x", status=JobStatus.APPLIED,
    )
    result = _find_overdue([applied_job], SAMPLE_SUBMISSIONS)
    assert TRACKER_JOB_ID_APPLIED_FUTURE not in result


def test_find_overdue_ignores_non_applied_status():
    """A job with a past followup_due_date but status=rejected is not overdue."""
    from pipeline.agents.tracker import _find_overdue
    from pipeline.state import JobListing, JobStatus

    rejected_job = JobListing(
        id=TRACKER_JOB_ID_APPLIED,  # same sub-001 (past due) but job is rejected
        source="indeed", title="T", company="C", location="L",
        source_url="http://x", status=JobStatus.REJECTED,
    )
    result = _find_overdue([rejected_job], SAMPLE_SUBMISSIONS)
    assert TRACKER_JOB_ID_APPLIED not in result


# ── Days-since formatting ─────────────────────────────────────────────────────


def test_format_days_since_today():
    """Today's ISO date string returns 'today'."""
    from pipeline.agents.tracker import _format_days_since
    assert _format_days_since(date.today().isoformat()) == "today"


def test_format_days_since_past():
    """An ISO date 7 days ago returns '7 days ago'."""
    from pipeline.agents.tracker import _format_days_since
    seven_ago = (date.today() - timedelta(days=7)).isoformat()
    assert _format_days_since(seven_ago) == "7 days ago"


# ── Node behavior ─────────────────────────────────────────────────────────────


def test_load_pipeline_populates_shortlist(tracker_db):
    """load_pipeline queries active jobs from DB and returns them in shortlist."""
    from pipeline.agents.tracker import load_pipeline
    from pipeline.state import empty_state

    state = empty_state()
    with patch("pipeline.agents.tracker.settings") as mock_settings:
        mock_settings.app_db_path = tracker_db
        result = load_pipeline(state)

    # tracker_db has 3 jobs; rejected is excluded from active statuses
    shortlist = result.get("shortlist", [])
    assert len(shortlist) == 2
    ids = {j.id for j in shortlist}
    assert TRACKER_JOB_ID_APPLIED in ids
    assert TRACKER_JOB_ID_APPLIED_FUTURE in ids
    assert TRACKER_JOB_ID_REJECTED not in ids


def test_update_status_writes_to_db(tracker_db):
    """update_status advances job status in DB and sets updated_at."""
    from pipeline.agents.tracker import update_status
    from pipeline.state import JobListing, JobStatus, empty_state

    job = JobListing(
        id=TRACKER_JOB_ID_APPLIED,
        source="indeed", title="T", company="C", location="L",
        source_url="http://x", status=JobStatus.APPLIED,
    )
    state = empty_state()
    state["shortlist"] = [job]
    state["current_job_id"] = TRACKER_JOB_ID_APPLIED
    state["tracker_new_status"] = "rejected"

    with patch("pipeline.agents.tracker.settings") as mock_settings:
        mock_settings.app_db_path = tracker_db
        update_status(state)

    conn = sqlite3.connect(tracker_db)
    row = conn.execute(
        "SELECT status, updated_at FROM jobs WHERE id = ?",
        (TRACKER_JOB_ID_APPLIED,),
    ).fetchone()
    conn.close()

    assert row[0] == "rejected"
    assert row[1] is not None  # updated_at was written
