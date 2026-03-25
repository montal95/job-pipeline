-- Migration 004: add UNIQUE constraint to jobs.fingerprint
-- Required for ON CONFLICT(fingerprint) upsert in persist_to_db.
-- Deletes any rows with empty fingerprints from broken earlier runs before
-- creating the index to avoid constraint violations on blank values.

DELETE FROM jobs WHERE fingerprint = '' OR fingerprint IS NULL;

DROP INDEX IF EXISTS idx_jobs_fingerprint;
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(fingerprint);
