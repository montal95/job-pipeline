-- Migration 005: company_scans — tracks scanner runs per company
--
-- Each row records a single invocation of scrape_target_companies for one
-- company. Lets us show "last scanned" timestamps in the tracker dashboard
-- and debug flaky ATS endpoints (zero-result scans over time).

CREATE TABLE IF NOT EXISTS company_scans (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name     TEXT    NOT NULL,
    ats_type         TEXT    NOT NULL,            -- greenhouse | ashby | lever
    last_scanned_at  TEXT    NOT NULL,            -- ISO-8601 UTC
    jobs_found       INTEGER NOT NULL DEFAULT 0,
    error            TEXT                          -- populated on scan failure
);

CREATE INDEX IF NOT EXISTS idx_company_scans_name_time
    ON company_scans (company_name, last_scanned_at DESC);

-- down:
-- DROP INDEX IF EXISTS idx_company_scans_name_time;
-- DROP TABLE IF EXISTS company_scans;
