-- Migration 003: add updated_at column to jobs table
-- Tracker agent writes this when advancing a job's status via update_status node.

ALTER TABLE jobs ADD COLUMN updated_at TEXT;
