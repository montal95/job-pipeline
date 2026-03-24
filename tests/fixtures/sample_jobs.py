"""
Synthetic job listing fixtures for unit tests.

These are anonymized, realistic-looking listings that exercise
all code paths in the Discoverer without hitting live job boards.
"""

from pipeline.state import RawJobListing, WorkplaceType

INDEED_FIXTURES: list[RawJobListing] = [
    RawJobListing(
        source="indeed",
        title="Senior Software Engineer",
        company="Acme Health",
        location="Chicago, IL",
        source_url="https://www.indeed.com/jobs/viewjob?jk=abc123",
        apply_url="https://boards.greenhouse.io/acmehealth/jobs/1",
        description="Rails API, PostgreSQL, AWS. 4+ years required.",
        compensation_low=140000,
        compensation_high=170000,
        workplace_type=WorkplaceType.HYBRID,
    ),
    RawJobListing(
        source="indeed",
        title="Full Stack Developer",
        company="TechStartup Inc",
        location="Remote",
        source_url="https://www.indeed.com/jobs/viewjob?jk=def456",
        apply_url="https://jobs.ashby.io/techstartup/apply",
        description="Node.js, React, TypeScript. 3+ years.",
        compensation_low=120000,
        compensation_high=150000,
        workplace_type=WorkplaceType.REMOTE,
    ),
    RawJobListing(
        source="indeed",
        title="Backend Engineer",
        company="Acme Health",          # same company as first — diff title, not a dup
        location="Chicago, IL",
        source_url="https://www.indeed.com/jobs/viewjob?jk=ghi789",
        workplace_type=WorkplaceType.HYBRID,
    ),
]

DICE_FIXTURES: list[RawJobListing] = [
    RawJobListing(
        source="dice",
        title="Senior Software Engineer",  # same as INDEED_FIXTURES[0] — IS a dup
        company="Acme Health",
        location="Chicago, IL",
        source_url="https://www.dice.com/job-detail/xyz001",
        apply_url="https://boards.greenhouse.io/acmehealth/jobs/1",
        description="Dice cross-posting of the Acme Health role.",
        workplace_type=WorkplaceType.HYBRID,
    ),
    RawJobListing(
        source="dice",
        title="AI Engineer",
        company="DataCo",
        location="New York, NY",
        source_url="https://www.dice.com/job-detail/xyz002",
        apply_url="https://dataco.workday.com/jobs/ai-engineer",
        description="LangChain, Python, AWS Bedrock.",
        compensation_low=160000,
        compensation_high=200000,
        workplace_type=WorkplaceType.REMOTE,
    ),
]

ALL_FIXTURES = INDEED_FIXTURES + DICE_FIXTURES
