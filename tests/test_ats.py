"""
ATS detection tests — detect_ats() URL fingerprinting.

Tests that apply URL patterns are correctly mapped to AtsType enum values.
Pure function, no I/O.
"""

from __future__ import annotations

from pipeline.ats import detect_ats
from pipeline.state import AtsType


def test_detect_ats_greenhouse():
    assert detect_ats("https://boards.greenhouse.io/acme/jobs/1") == AtsType.GREENHOUSE


def test_detect_ats_workday():
    assert detect_ats("https://acme.myworkdayjobs.com/en-US/jobs/1") == AtsType.WORKDAY


def test_detect_ats_ashby():
    assert detect_ats("https://jobs.ashby.io/acme/apply") == AtsType.ASHBY


def test_detect_ats_linkedin():
    assert detect_ats("https://www.linkedin.com/jobs/view/12345") == AtsType.LINKEDIN


def test_detect_ats_unknown_url():
    assert detect_ats("https://careers.somecompany.com/apply") == AtsType.OTHER


def test_detect_ats_none():
    assert detect_ats(None) == AtsType.UNKNOWN
