"""
Synthetic submission fixtures for Tracker agent unit tests.

Uses stable string IDs so submissions can be matched against jobs
in a seeded DB without relying on UUID generation.
"""

from __future__ import annotations

# Stable job IDs used across tracker fixtures and the tracker_db conftest fixture
TRACKER_JOB_ID_APPLIED = "tracker-job-001"
TRACKER_JOB_ID_APPLIED_FUTURE = "tracker-job-002"
TRACKER_JOB_ID_REJECTED = "tracker-job-003"

SAMPLE_SUBMISSIONS: list[dict] = [
    {
        "id": "sub-001",
        "job_id": TRACKER_JOB_ID_APPLIED,
        "submitted_at": "2026-03-10T10:00:00",
        "followup_due_date": "2026-03-10",  # past — overdue
        "followup_completed_at": None,
    },
    {
        "id": "sub-002",
        "job_id": TRACKER_JOB_ID_APPLIED_FUTURE,
        "submitted_at": "2026-03-20T10:00:00",
        "followup_due_date": "2099-04-15",  # far future — not due
        "followup_completed_at": None,
    },
]
