# Phase 5 Handoff — Tracker Agent

**Date:** 2026-03-24
**Branch:** main
**Test state:** 95/95 passing (Phases 0–4 complete, test suite refactored)
**Next phase:** Tracker agent — status dashboard, follow-up scheduling, end-to-end CLI wiring

---

## Current repo state

```
src/pipeline/
  agents/
    discoverer.py  ✅ Full implementation (Phases 1+2)
    writer.py      ✅ Full implementation (Phase 3, deferred-render refactor in Phase 4)
    submitter.py   ✅ Full implementation (Phase 4)
    tracker.py     🔜 Phase 0 stubs only
tests/
  conftest.py      ✅ Shared fixtures (refactored in Phase 4 cleanup)
  test_smoke.py    ✅ 4 graph compile tests
  test_state.py    ✅ 7 schema tests
  test_ats.py      ✅ 6 ATS detection tests
  test_config.py   ✅ 13 config helper tests
  test_discoverer.py ✅ 34 discoverer tests
  test_writer.py   ✅ 19 writer tests
  test_submitter.py ✅ 13 submitter tests
```

---

## Tracker agent — responsibility

The Tracker's job is purely operational: read the DB, render a status summary,
surface overdue follow-ups, and accept user commands to advance job statuses.
Zero LLM calls. Zero Playwright. Entirely DB + terminal rendering.

**What it does NOT do:**
- It does not scrape, generate documents, or submit forms
- It does not write to the checkpoint DB (no LangGraph state to persist here)
- It does not send emails or notifications (v2 scope)

---

## Tracker agent — topology

```
load_pipeline
      │
render_dashboard  ← Rich table: counts by status, overdue follow-ups
      │
 update_status    ← interactive: accept CLI args or prompts to move job statuses
      │
schedule_followup ← compute and write followup_due_date for submitted jobs
      │
  flag_overdue    ← surface jobs where followup_due_date has passed with no update
      │
     END
```

This is the simplest graph in the pipeline — linear, no interrupts, no cycles.
The LangGraph pattern exercised here is **graph as workflow orchestrator for
purely synchronous DB operations**, which is worth demonstrating explicitly
because it shows LangGraph doesn't require LLMs or async I/O to be useful.

---

## Node definitions

### `load_pipeline`

Query the DB for all jobs with non-terminal statuses (`new`, `queued`,
`docs_draft`, `docs_ready`, `submitted`, `applied`, `possibly_inactive`).
Also query the `submissions` table for follow-up due dates.

Returns: `shortlist` populated with all active jobs.

### `render_dashboard`

Render a Rich terminal table with:
- Status counts row: `new | queued | docs_ready | applied | rejected | offer`
- Per-job rows (for `applied` and `submitted`): company, title, applied_date,
  followup_due_date, days since application
- Overdue section: jobs where `followup_due_date < today` with no update

**Rich table structure (two tables):**

```
┌─────────────────── Pipeline Summary ───────────────────┐
│ new │ queued │ docs_ready │ applied │ rejected │ offer  │
│  3  │   2    │     1      │    4    │    1     │   0    │

┌──────────────── Active Applications ───────────────────────────────────────┐
│ Company         │ Role                   │ Applied    │ Follow-up  │ Status │
│ Acme Health     │ Senior Rails Engineer  │ 2026-03-20 │ 2026-03-27 │ ⚠ due  │
│ Startup Inc     │ Backend Engineer       │ 2026-03-22 │ 2026-03-29 │ ok     │
```

### `update_status`

Accept a job_id and new status from CLI args or interactive prompt.
Valid transitions: any status → any status (user-driven, no guards).
Write updated status + `updated_at` to the `jobs` table.

### `schedule_followup`

For any job at status `applied` or `submitted` that has no `followup_due_date`,
compute `submitted_at + 7 days` and write it to the `submissions` table.
Idempotent — skip if `followup_due_date` already set.

### `flag_overdue`

Query submissions where `followup_due_date < today` and the job's status is
still `applied` (not yet updated to `rejected`, `offer`, etc.).
Append a warning to state for each overdue job.
These are surfaced in `render_dashboard` with a ⚠ indicator.

---

## New pure functions (unit testable)

```python
# tracker.py

def _count_by_status(jobs: list[JobListing]) -> dict[str, int]:
    """Return a dict of status → count for a list of JobListings."""

def _find_overdue(jobs: list[JobListing], submissions: list[dict]) -> list[str]:
    """
    Return job IDs where followup_due_date has passed and status is still 'applied'.
    submissions is a list of dicts from the submissions table.
    """

def _format_days_since(date_str: str) -> str:
    """
    Return a human-readable string like '3 days ago' or 'today'
    given an ISO date string.
    """
```

---

## DB queries needed

All queries use sync `sqlite3` — same pattern as writer and submitter.

```sql
-- load_pipeline: active jobs
SELECT * FROM jobs
WHERE status NOT IN ('rejected', 'offer', 'skipped')
ORDER BY discovered_at DESC;

-- load follow-up data
SELECT s.job_id, s.followup_due_date, s.submitted_at
FROM submissions s
JOIN jobs j ON j.id = s.job_id
WHERE j.status IN ('submitted', 'applied');

-- update_status
UPDATE jobs SET status = ?, updated_at = datetime('now') WHERE id = ?;

-- schedule_followup
UPDATE submissions
SET followup_due_date = date(submitted_at, '+7 days')
WHERE job_id = ?
  AND followup_due_date IS NULL;

-- flag_overdue
SELECT s.job_id FROM submissions s
JOIN jobs j ON j.id = s.job_id
WHERE s.followup_due_date < date('now')
  AND j.status = 'applied';
```

Note: `jobs` table doesn't currently have an `updated_at` column. Add it in
migration 003.

---

## Migration 003

New file: `migrations/003_updated_at_column.sql`

```sql
ALTER TABLE jobs ADD COLUMN updated_at TEXT;
```

---

## `pipeline run` command

Phase 5 also wires the full end-to-end `pipeline run` CLI command, which chains
all four agents: discover → write (for approved jobs) → submit → track.

The current `_run()` stub in cli.py should be replaced with:

```python
async def _run(query, location, sources, remote):
    # 1. discover
    await _discover(query, location, sources, remote)
    # 2. for each queued job, write + submit
    # 3. track
    await _track()
```

The write+submit loop queries the DB for jobs at status `queued` after
discovery and calls `_write(job_id)` + `_submit(job_id)` for each one that
the user approved during triage.

---

## Test plan (TDD — write tests first)

### Commit 1 — `db: migration 003 updated_at column`

New file: `migrations/003_updated_at_column.sql`
No new tests. Verify it applies cleanly.

---

### Commit 2 — `test(tracker): fixtures + 9 failing tests`

**New fixture file:** `tests/fixtures/sample_submissions.py`

```python
# A list of dicts matching the submissions table schema:
# id, job_id, submitted_at, followup_due_date, followup_completed_at
SAMPLE_SUBMISSIONS = [
    {
        "id": "sub-001",
        "job_id": "<matches a INDEED_FIXTURES[0] id>",
        "submitted_at": "2026-03-17T10:00:00",
        "followup_due_date": "2026-03-10",   # overdue
        "followup_completed_at": None,
    },
    {
        "id": "sub-002",
        "job_id": "<matches a DICE_FIXTURES[0] id>",
        "submitted_at": "2026-03-20T10:00:00",
        "followup_due_date": "2026-04-15",   # not yet due
        "followup_completed_at": None,
    },
]
```

**New test file:** `tests/test_tracker.py` — 9 tests, all red.

```
Status counting — 2 tests:
  test_count_by_status_basic
    Given a list of 4 jobs with mixed statuses, returns correct counts per status
  test_count_by_status_empty
    Empty list returns empty dict

Overdue detection — 3 tests:
  test_find_overdue_returns_past_due_jobs
    sub-001 has followup_due_date in the past + status=applied → returned
  test_find_overdue_ignores_future_due_date
    sub-002 has future followup_due_date → not returned
  test_find_overdue_ignores_non_applied_status
    Job with past followup_due_date but status=rejected → not returned

Days-since formatting — 2 tests:
  test_format_days_since_today
    Today's ISO date → 'today'
  test_format_days_since_past
    7 days ago → '7 days ago'

Node behavior — 2 tests:
  test_load_pipeline_populates_shortlist
    Seeded DB with 2 jobs → load_pipeline returns both in shortlist
  test_update_status_writes_to_db
    Call update_status with job_id + new_status → DB row updated
```

---

### Commit 3 — `feat(tracker): pure functions`

Implement `_count_by_status`, `_find_overdue`, `_format_days_since`.
Pure — no DB, no Rich, no LangGraph.

Expected green after this commit: 7/9 tests (counting + overdue + days-since).

---

### Commit 4 — `feat(tracker): load_pipeline + update_status nodes`

Implement `load_pipeline` and `update_status` with sync sqlite3.

All 9 tracker tests green. **95 + 9 = 104/104.**

---

### Commit 5 — `feat(tracker): render_dashboard + schedule_followup + flag_overdue`

Implement `render_dashboard` (Rich tables), `schedule_followup`,
`flag_overdue`. No new tests for these — Rich output is not unit-tested
(same rationale as Playwright nodes: terminal output is an integration concern).

---

### Commit 6 — `feat(cli): wire pipeline run end-to-end`

Implement `_run()` in cli.py: discover → write queued jobs → submit →
track. No new tests.

---

### Commit 7 — `docs: README Phase 5 update`

- Status: Phase 5 complete, 104/104 tests
- Phase table: Phase 5 ✅, Phase 6 🔜
- Project structure: tracker.py ✅
- LangGraph patterns: add "graph as DB workflow orchestrator" entry

---

## Files created or modified in Phase 5

| File | Action |
|------|--------|
| `migrations/003_updated_at_column.sql` | New |
| `src/pipeline/agents/tracker.py` | Full implementation |
| `src/pipeline/cli.py` | Wire `_run()` end-to-end |
| `tests/fixtures/sample_submissions.py` | New |
| `tests/test_tracker.py` | New — 9 tests |
| `README.md` | Updated |

**Expected final test count: 104/104** (95 existing + 9 new)
