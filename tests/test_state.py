"""
State schema tests — PipelineState TypedDict, Pydantic models, enums, empty_state().

Verifies that the core data contracts are correctly typed and that empty_state()
produces a valid zero-value initial state. No external services, no DB, no graph.
"""

from __future__ import annotations

import pytest

from pipeline.state import (
    AtsType,
    FitSignal,
    JobListing,
    JobStatus,
    PipelineState,
    RawJobListing,
    SearchParams,
    SubmissionStatus,
    WorkplaceType,
    empty_state,
)


def test_empty_state_is_valid():
    state = empty_state()
    assert isinstance(state, dict)
    assert state["raw_results"] == []
    assert state["shortlist"] == []
    assert state["human_approved"] is False
    assert state["revision_round"] == 0
    assert state["needs_file_upload"] is False
    assert state["ats_field_map"] == {}
    assert state["submission_confirmed"] is False


def test_search_params_defaults():
    params = SearchParams(query="rails engineer", location="Chicago, IL")
    assert params.remote is True
    assert params.max_results_per_source == 25
    # Indeed dropped in F5-T6 (flaky scraper); builtin + target_companies added.
    assert "dice" in params.sources
    assert "target_companies" in params.sources
    assert "indeed" not in params.sources


def test_raw_job_listing_minimal():
    listing = RawJobListing(
        source="indeed",
        title="Senior Software Engineer",
        company="Acme Corp",
        location="Chicago, IL",
        source_url="https://indeed.com/jobs/123",
    )
    assert listing.apply_url is None
    assert listing.compensation_low is None


def test_job_listing_has_id():
    listing = JobListing(
        source="dice",
        title="Rails Engineer",
        company="Startup Inc",
        location="Remote",
        source_url="https://dice.com/jobs/456",
    )
    assert listing.id
    assert listing.status == JobStatus.NEW
    assert listing.ats_type == AtsType.UNKNOWN


def test_submission_status_defaults():
    status = SubmissionStatus()
    assert status.submitted is False
    assert status.error is None


def test_job_status_enum_values():
    assert JobStatus.NEW == "new"
    assert JobStatus.APPLIED == "applied"
    assert JobStatus.POSSIBLY_INACTIVE == "possibly_inactive"


def test_fit_signal_enum_values():
    assert FitSignal.STRONG == "strong"
    assert FitSignal.STRETCH == "stretch"
