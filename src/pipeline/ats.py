"""
ATS detection utilities — shared across Discoverer and Submitter agents.

Moved here from discoverer.py because ATS fingerprinting is domain logic
used by multiple agents. Keeping it in discoverer.py would require importing
a private symbol (_detect_ats) across module boundaries.
"""

from __future__ import annotations

from pipeline.state import AtsType


ATS_PATTERNS: dict[str, list[str]] = {
    "greenhouse": ["greenhouse.io", "boards.greenhouse.io"],
    "workday": ["workday.com", "myworkdayjobs.com"],
    "ashby": ["ashbyhq.com", "jobs.ashby"],
    "linkedin": ["linkedin.com/jobs"],
}


def detect_ats(url: str | None) -> AtsType:
    """Fingerprint a URL to determine which ATS platform it belongs to."""
    if not url:
        return AtsType.UNKNOWN
    url_lower = url.lower()
    for ats, patterns in ATS_PATTERNS.items():
        if any(p in url_lower for p in patterns):
            return AtsType(ats)
    return AtsType.OTHER
