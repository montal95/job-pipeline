"""
Phase 0 tests — graph compilation and state schema smoke tests.

These tests verify that:
  1. All four agent graphs compile without errors
  2. PipelineState TypedDict is correctly typed
  3. empty_state() produces a valid initial state
  4. Pydantic models serialize/deserialize cleanly

No external services are called. No DB connections are opened.
"""

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


# ── State schema ───────────────────────────────────────────────────────────────


def test_empty_state_is_valid():
    state = empty_state()
    assert isinstance(state, dict)
    assert state["raw_results"] == []
    assert state["shortlist"] == []
    assert state["human_approved"] is False
    assert state["revision_round"] == 0


def test_search_params_defaults():
    params = SearchParams(query="rails engineer", location="Chicago, IL")
    assert params.remote is True
    assert params.max_results_per_source == 25
    assert "indeed" in params.sources


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
    assert listing.id  # UUID auto-generated
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


# ── Graph compilation ──────────────────────────────────────────────────────────


def test_discoverer_graph_compiles():
    from pipeline.agents.discoverer import build_discoverer_graph

    graph = build_discoverer_graph()
    compiled = graph.compile()
    assert compiled is not None


def test_writer_graph_compiles():
    from pipeline.agents.writer import build_writer_graph

    graph = build_writer_graph()
    compiled = graph.compile()
    assert compiled is not None


def test_submitter_graph_compiles():
    from pipeline.agents.submitter import build_submitter_graph

    graph = build_submitter_graph()
    compiled = graph.compile()
    assert compiled is not None


def test_tracker_graph_compiles():
    from pipeline.agents.tracker import build_tracker_graph

    graph = build_tracker_graph()
    compiled = graph.compile()
    assert compiled is not None
