"""
Central state schema for the job application pipeline.

All agents read and write PipelineState. LangGraph's SqliteSaver persists
this between runs using thread IDs, enabling resume-from-failure.

The Pydantic models here serve two roles:
  1. Typed containers for data flowing through the graph
  2. DB row representations (serialized to/from SQLite as JSON)
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import TypedDict
from uuid import uuid4

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────────────────────


class JobStatus(str, Enum):
    NEW = "new"
    QUEUED = "queued"
    DOCS_DRAFT = "docs_draft"
    DOCS_READY = "docs_ready"
    SUBMITTED = "submitted"
    APPLIED = "applied"
    REJECTED = "rejected"
    OFFER = "offer"
    SKIPPED = "skipped"
    POSSIBLY_INACTIVE = "possibly_inactive"


class FitSignal(str, Enum):
    STRONG = "strong"
    PARTIAL = "partial"
    STRETCH = "stretch"


class AtsType(str, Enum):
    GREENHOUSE = "greenhouse"
    WORKDAY = "workday"
    ASHBY = "ashby"
    LINKEDIN = "linkedin"
    OTHER = "other"
    UNKNOWN = "unknown"


class WorkplaceType(str, Enum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"


# ── Search ─────────────────────────────────────────────────────────────────────


class SearchParams(BaseModel):
    query: str
    location: str
    sources: list[str] = Field(
        default_factory=lambda: ["indeed", "dice", "linkedin", "ziprecruiter"]
    )
    remote: bool = True
    max_results_per_source: int = 25


# ── Job listings ───────────────────────────────────────────────────────────────


class RawJobListing(BaseModel):
    """Unvalidated scraped listing. No ID yet; may have missing fields."""

    source: str
    title: str
    company: str
    location: str
    source_url: str
    apply_url: str | None = None
    description: str | None = None
    compensation_low: int | None = None
    compensation_high: int | None = None
    posted_date: date | None = None
    workplace_type: WorkplaceType | None = None


class JobListing(RawJobListing):
    """Normalized, deduped listing with a stable ID and enrichment fields."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    fingerprint: str = ""  # (company, title, location) hash — set by merge_results
    ats_type: AtsType = AtsType.UNKNOWN
    fit_signal: FitSignal | None = None
    company_headcount: int | None = None
    status: JobStatus = JobStatus.NEW
    discovered_at: datetime = Field(default_factory=datetime.utcnow)
    resume_path: str | None = None
    cover_letter_path: str | None = None
    notes: str | None = None


# ── Submission ─────────────────────────────────────────────────────────────────


class SubmissionStatus(BaseModel):
    submitted: bool = False
    submitted_at: datetime | None = None
    confirmation_text: str | None = None
    confirmation_screenshot_path: str | None = None
    followup_due_date: date | None = None
    error: str | None = None


# ── Pipeline state ─────────────────────────────────────────────────────────────


class PipelineState(TypedDict):
    # Search context
    search_params: SearchParams
    raw_results: list[RawJobListing]

    # Triage
    shortlist: list[JobListing]
    skipped: list[JobListing]

    # Document generation
    current_job_id: str | None
    interview_answers: dict[str, str]
    resume_path: str | None
    cover_letter_path: str | None
    revision_round: int

    # Submission
    submission_status: SubmissionStatus | None
    submission_url: str | None

    # Human-in-the-loop signals
    human_approved: bool
    human_feedback: str | None

    # Errors and diagnostics
    errors: list[str]
    warnings: list[str]


def empty_state() -> PipelineState:
    """Return a zeroed PipelineState suitable as a graph initial input."""
    return PipelineState(
        search_params=SearchParams(query="", location=""),
        raw_results=[],
        shortlist=[],
        skipped=[],
        current_job_id=None,
        interview_answers={},
        resume_path=None,
        cover_letter_path=None,
        revision_round=0,
        submission_status=None,
        submission_url=None,
        human_approved=False,
        human_feedback=None,
        errors=[],
        warnings=[],
    )
