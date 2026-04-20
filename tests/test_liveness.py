"""
Red-first tests for the liveness classifier pure function.

_classify_liveness() decides whether a job posting is active, expired, or
uncertain given the signals collected by check_job_liveness: http status,
the final URL after redirects, page body text, and any apply-button
controls detected in the DOM.

Priority (documented in F4-T3 card):
  1. 404 / 410 → expired
  2. Expired phrase in body → expired
  3. Final URL redirected away from the job path → expired
  4. Apply controls present → active
  5. Default → uncertain
"""

from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_classify_expired_phrase_in_body():
    from pipeline.agents.liveness import _classify_liveness
    body = _load("liveness_expired.html")
    result = _classify_liveness(
        http_status=200,
        final_url="https://acme.example.com/jobs/123",
        body_text=body,
        apply_controls=[],
        original_url="https://acme.example.com/jobs/123",
    )
    assert result == "expired"


def test_classify_active_apply_button_present():
    from pipeline.agents.liveness import _classify_liveness
    body = _load("liveness_active.html")
    result = _classify_liveness(
        http_status=200,
        final_url="https://acme.example.com/jobs/123",
        body_text=body,
        apply_controls=["Apply for this job"],
        original_url="https://acme.example.com/jobs/123",
    )
    assert result == "active"


def test_classify_uncertain_no_clear_signal():
    from pipeline.agents.liveness import _classify_liveness
    body = _load("liveness_uncertain.html")
    result = _classify_liveness(
        http_status=200,
        final_url="https://acme.example.com/careers",
        body_text=body,
        apply_controls=[],
        original_url="https://acme.example.com/careers",
    )
    assert result == "uncertain"


def test_classify_expired_redirect_to_careers_root():
    """Final URL no longer contains the job path → treat as expired."""
    from pipeline.agents.liveness import _classify_liveness
    result = _classify_liveness(
        http_status=200,
        final_url="https://acme.example.com/careers",
        body_text="<html><body>All open roles</body></html>",
        apply_controls=[],
        original_url="https://acme.example.com/jobs/abc-123",
    )
    assert result == "expired"


def test_classify_expired_404_status():
    from pipeline.agents.liveness import _classify_liveness
    result = _classify_liveness(
        http_status=404,
        final_url="https://acme.example.com/jobs/abc-123",
        body_text="<html><body>Not Found</body></html>",
        apply_controls=[],
        original_url="https://acme.example.com/jobs/abc-123",
    )
    assert result == "expired"


def test_classify_active_overrides_uncertain_body_when_apply_button_present():
    """Generic page text but apply controls exist → active wins over uncertain."""
    from pipeline.agents.liveness import _classify_liveness
    body = _load("liveness_uncertain.html")
    result = _classify_liveness(
        http_status=200,
        final_url="https://acme.example.com/jobs/xyz",
        body_text=body,
        apply_controls=["Apply now"],
        original_url="https://acme.example.com/jobs/xyz",
    )
    assert result == "active"


def test_classify_expired_410_gone():
    """410 Gone is an explicit expiration signal — locks the priority spec."""
    from pipeline.agents.liveness import _classify_liveness
    result = _classify_liveness(
        http_status=410,
        final_url="https://acme.example.com/jobs/abc-123",
        body_text="<html><body>Gone</body></html>",
        apply_controls=[],
        original_url="https://acme.example.com/jobs/abc-123",
    )
    assert result == "expired"
