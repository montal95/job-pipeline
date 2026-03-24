-- Migration 001: initial schema
-- Run via: python -m pipeline.database migrate

-- ── jobs ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS jobs (
    id                  TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    company             TEXT NOT NULL,
    location            TEXT NOT NULL,
    workplace_type      TEXT,                          -- remote/hybrid/onsite
    source              TEXT NOT NULL,                 -- indeed/dice/linkedin/ziprecruiter
    source_url          TEXT NOT NULL,
    apply_url           TEXT,
    ats_type            TEXT DEFAULT 'unknown',        -- greenhouse/workday/ashby/linkedin/unknown
    description         TEXT,
    compensation_low    INTEGER,                       -- annualized, nullable
    compensation_high   INTEGER,                       -- annualized, nullable
    posted_date         TEXT,                          -- ISO date string, nullable
    discovered_at       TEXT NOT NULL,                 -- ISO datetime
    status              TEXT NOT NULL DEFAULT 'new',   -- see JobStatus enum
    fit_signal          TEXT,                          -- strong/partial/stretch
    company_headcount   INTEGER,
    fingerprint         TEXT NOT NULL DEFAULT '',      -- (company, title, location) hash
    resume_path         TEXT,
    cover_letter_path   TEXT,
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status       ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint  ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS idx_jobs_company      ON jobs(company);

-- ── submissions ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS submissions (
    id                              TEXT PRIMARY KEY,
    job_id                          TEXT NOT NULL REFERENCES jobs(id),
    submitted_at                    TEXT NOT NULL,     -- ISO datetime
    confirmation_text               TEXT,
    confirmation_screenshot_path    TEXT,
    followup_due_date               TEXT,              -- ISO date string
    followup_completed_at           TEXT               -- ISO datetime, nullable
);

CREATE INDEX IF NOT EXISTS idx_submissions_job_id ON submissions(job_id);

-- ── search_runs ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS search_runs (
    id                  TEXT PRIMARY KEY,
    run_at              TEXT NOT NULL,                 -- ISO datetime
    params_json         TEXT NOT NULL,                 -- serialized SearchParams
    sources_used        TEXT NOT NULL,                 -- comma-separated
    total_raw_results   INTEGER NOT NULL DEFAULT 0,
    total_shortlisted   INTEGER NOT NULL DEFAULT 0,
    total_approved      INTEGER NOT NULL DEFAULT 0,
    seen_job_ids        TEXT NOT NULL DEFAULT '[]'     -- JSON array of fingerprints
);

-- ── company_cache ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS company_cache (
    company_name    TEXT PRIMARY KEY,
    headcount       INTEGER,
    mission         TEXT,
    recent_news     TEXT,
    cached_at       TEXT NOT NULL                      -- ISO datetime
);
