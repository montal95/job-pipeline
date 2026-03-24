-- Migration 002: add content JSON columns to jobs table
-- Writer now persists structured content as JSON instead of rendering docx immediately.
-- Submitter reads these columns and renders .docx only when the ATS form has a file input.

ALTER TABLE jobs ADD COLUMN resume_content_json TEXT;
ALTER TABLE jobs ADD COLUMN cover_letter_content_json TEXT;
