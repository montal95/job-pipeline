# Phase 4 Handoff — Submitter Agent

**Date:** 2026-03-24
**Branch:** main
**Test state:** 82/82 passing (Phase 0–3 complete)
**Next phase:** Submitter agent — ATS form fill, conditional docx rendering, hard submission gate

---

## Current repo state

```
src/pipeline/
  ats.py          ✅ Shared ATS detection — ATS_PATTERNS + detect_ats()
  database.py     ✅ Migration runner — handles partial-run idempotency
  agents/
    discoverer.py ✅ Full implementation (Phases 1+2) — imports from pipeline.ats
    writer.py     ✅ Full implementation (Phase 3) — MODIFIED in Phase 4 (see below)
    submitter.py  🔜 Phase 0 stubs
    tracker.py    🔜 Phase 0 stubs
migrations/
  001_initial.sql ✅ Initial schema
docs/
  phase3-handoff.md ← previous handoff
  phase4-handoff.md ← this file
```

---

## Key architectural decision: deferred docx rendering (Option B)

Phase 3 Writer renders docx files immediately inside `write_resume` and
`write_cover_letter`. This is premature — the Submitter is the agent that
actually knows whether a file upload input exists on the ATS form.

**Phase 4 changes this:**

- Writer generates and persists `ResumeContent` / `CoverLetterContent` as JSON
  to the `jobs` table. No `.docx` is produced during the Writer phase.
- `review_interrupt` shows a Rich terminal preview of the structured content
  instead of file paths. The user approves tailored *content*, not an artifact.
- Submitter's `render_documents_if_needed` node reads content JSON from the DB
  and renders to `.docx` only after confirming a file upload input is present
  in the ATS form. If no file input is detected, rendering is skipped entirely.

**Known v1 limitation:** the user approves a terminal text preview of the resume,
not the rendered document. A "preview render" producing a temp `.docx` for review
is a v2 improvement path.

---

## Writer modifications required (Phase 4 touches Phase 3 code)

These changes happen in Commit 1 and are required before building the Submitter.

### `write_resume` and `write_cover_letter`
- Remove `_render_resume_docx` and `_render_cover_letter_docx` calls
- Return `resume_content` and `cover_letter_content` models only
- Do NOT return `resume_path` / `cover_letter_path`

### `review_interrupt`
- Replace file path display with Rich-formatted preview of `resume_content`:
  name, summary (truncated to 300 chars), section headings + bullet counts
- Replace interrupt data keys `resume_path`/`cover_letter_path` with
  `resume_preview`/`cover_letter_preview` (plain text strings)

### `persist_documents`
- Serialize `resume_content` and `cover_letter_content` to JSON strings
- Write to new DB columns `resume_content_json` and `cover_letter_content_json`
- File path columns (`resume_path`, `cover_letter_path`) remain in schema but
  stay NULL until Submitter renders (if ever)

### Existing test update (same commit — keeps suite green)
`test_write_resume_calls_llm_once` currently asserts `result.get("resume_path") is not None`.
Change to: `assert result.get("resume_content") is not None`

Suite must be **82/82 green** after Commit 1.


---

## Submitter agent — topology

```
load_submission_job
        │
   detect_ats
        │
   scan_form
        │
  needs_file_upload?
    yes │        no
        ▼         ▼
render_documents  (skip)
        │         │
        └────┬────┘
             ▼
         fill_form
             │
    submission_gate (interrupt — hard gate, no form submitted without 'yes')
             │
    ┌────────┴────────────┐
   yes                 abort
    │                    │
 submit_form     abort_submission → END
    │
capture_confirmation
    │
persist_submission → END
```

`scan_form` sets `state["needs_file_upload"]: bool`.
`render_documents_if_needed` branches on that flag.
`fill_form` skips the file attachment step if `resume_path` is None.

---

## New pure functions (unit testable)

```python
# submitter.py

def _has_file_input(html: str) -> bool
    """Return True if form HTML contains any <input type="file"> element,
    regardless of CSS visibility. Hidden file inputs are still accessible
    via Playwright's file_upload tool."""

def _map_form_fields(html: str, ats_type: AtsType) -> dict[str, str]
    """Extract field label → CSS selector mappings from ATS form HTML.
    Greenhouse: <label for="X"> → associated input by ID → {"label_text": "#X"}
    Workday: aria-label attributes → {"label_text": "[aria-label='X']"}
    File inputs excluded — handled separately by render_documents_if_needed."""

def _parse_confirmation(html: str) -> bool
    """Return True if post-submit page HTML contains success indicators.
    Checks (case-insensitive): 'application submitted', 'thank you',
    'we received your', 'confirmation number'.
    Returns False if error patterns found or no success signal present."""

def _render_documents_if_needed(state: PipelineState) -> dict
    """Read resume_content_json + cover_letter_content_json from DB.
    If needs_file_upload is True: deserialize, call _render_resume_docx and
    _render_cover_letter_docx (imported from writer.py), return paths.
    If needs_file_upload is False: return {} (no-op, no files created).
    If content JSON missing from DB: append to errors, return {}."""

def should_submit(state: PipelineState) -> str
    """Routing function for post-scan conditional edge.
    'fill_form' — no errors, ats_field_map populated
    'abort'     — errors non-empty"""
```

---

## New state fields (add to `state.py`)

```python
class PipelineState(TypedDict):
    # ... existing fields ...
    needs_file_upload: bool          # set by scan_form; drives render node
    ats_field_map: dict[str, str]    # label → selector, set by scan_form
    submission_confirmed: bool       # set by capture_confirmation
```

---

## DB changes: migration 002

New file: `migrations/002_content_json_columns.sql`

```sql
ALTER TABLE jobs ADD COLUMN resume_content_json TEXT;
ALTER TABLE jobs ADD COLUMN cover_letter_content_json TEXT;
```

`resume_path` and `cover_letter_path` columns remain. They stay NULL until
Submitter renders (if ever). Writer no longer writes to them.


---

## ATS strategies (v1 scope)

| ATS | Strategy | Notes |
|-----|----------|-------|
| Greenhouse | Full implementation | Stable DOM; label-based field mapping reliable |
| Workday | Manual fallback | Session drops on programmatic nav; surface warning with URL, user navigates manually |
| Ashby | Stub | DOM research needed before implementation |
| LinkedIn Easy Apply | Stub | Requires active auth session; different interaction pattern |
| Unknown | Warn + surface URL | User handles manually |

---

## Test plan (TDD — write tests first, all red, then implement)

### Commit 1 — `refactor(writer): strip docx rendering, persist content JSON`

**Modified files:** `src/pipeline/agents/writer.py`, `tests/test_phase3.py`

Implement all writer modifications described above. Update existing test:
- `test_write_resume_calls_llm_once`: assert `resume_content is not None`
  (remove `resume_path` assertion)

**Suite must be 82/82 green after this commit.**

---

### Commit 2 — `db: add migration 002 for content JSON columns`

**New file:** `migrations/002_content_json_columns.sql`

No new tests. Verify migration applies cleanly against a fresh DB.

---

### Commit 3 — `test(phase4): fixture data`

**New fixture files:**

- `tests/fixtures/greenhouse_form.html` — Greenhouse apply form with file inputs:
  resume (`<input type="file" id="resume">`), cover letter, plus text fields
  (first_name, last_name, email). Must also include a hidden file input to
  exercise the visibility edge case.

- `tests/fixtures/greenhouse_form_no_upload.html` — Same form structure,
  all `<input type="file">` elements removed.

- `tests/fixtures/workday_form.html` — Minimal Workday DOM with `aria-label`
  attributes. No file upload input (Workday uses separate resume parser UI).

- `tests/fixtures/greenhouse_confirmation.html` — Post-submit success page
  containing "application submitted" and a confirmation number.

- `tests/fixtures/submission_error.html` — Generic error page, no success signals.

---

### Commit 4 — `test(phase4): 15 failing tests`

**New file:** `tests/test_phase4.py` — all red on first run.

**Writer regression — 2 tests:**
```
test_write_resume_returns_content_not_path
  write_resume result has resume_content set, resume_path is None

test_persist_documents_saves_content_json
  After persist_documents, DB row has resume_content_json populated,
  resume_path is still NULL
  (uses tmp SQLite DB seeded with a jobs row via migration 001+002)
```

**File input detection — 3 tests:**
```
test_has_file_input_detects_present
  greenhouse_form.html → True

test_has_file_input_detects_absent
  greenhouse_form_no_upload.html → False

test_has_file_input_detects_hidden_input
  HTML with <input type="file" style="display:none"> → True
```

**ATS field mapping — 3 tests:**
```
test_map_form_fields_greenhouse_happy_path
  greenhouse_form.html + AtsType.GREENHOUSE →
  {"first_name": "#first_name", "last_name": "#last_name", "email": "#email"}
  (file inputs excluded from field map)

test_map_form_fields_greenhouse_empty_form
  <form></form> → {}

test_map_form_fields_workday_happy_path
  workday_form.html + AtsType.WORKDAY → dict keyed by aria-label values
```


**Conditional render node — 3 tests:**
```
test_render_documents_if_needed_renders_when_upload_required
  state: needs_file_upload=True, resume_content_json + cover_letter_content_json in DB
  Result: resume_path and cover_letter_path non-None; .docx files exist at paths
  (uses tmp SQLite DB with migration applied + tmp_path for output dir)

test_render_documents_if_needed_skips_when_no_upload
  state: needs_file_upload=False
  Result: {} — no files created, no paths set

test_render_documents_if_needed_errors_on_missing_content
  state: needs_file_upload=True, content JSON absent from DB
  Result: errors list contains missing-content message
```

**Confirmation detection — 2 tests:**
```
test_parse_confirmation_detects_success
  greenhouse_confirmation.html → True

test_parse_confirmation_detects_error_page
  submission_error.html → False
```

**Submission routing — 2 tests:**
```
test_should_submit_routes_to_fill_form_when_ready
  state: ats_field_map populated, errors=[] → "fill_form"

test_should_submit_routes_to_abort_on_error
  state: errors=["something went wrong"] → "abort"
```

**Total: 15 new tests, all red at commit time.**

---

### Commit 5 — `feat(submitter): pure functions`

**File:** `src/pipeline/agents/submitter.py`

Implement `_has_file_input`, `_map_form_fields`, `_parse_confirmation`.
All pure — no Playwright, no DB, no LLM. Use `html.parser` via `BeautifulSoup4`
(already a project dependency from Phase 1).

Expected green after this commit: file input (3) + field mapping (3) + confirmation (2) = **8/15**.

---

### Commit 6 — `feat(submitter): render_documents_if_needed + routing`

**File:** `src/pipeline/agents/submitter.py`

Implement `_render_documents_if_needed` and `should_submit`.

`_render_documents_if_needed`:
- Reads `resume_content_json` / `cover_letter_content_json` from `jobs` table
  via sync `sqlite3` (same pattern as `load_job` in writer.py)
- Deserializes via `ResumeContent.model_validate_json()` /
  `CoverLetterContent.model_validate_json()`
- Calls `_render_resume_docx` and `_render_cover_letter_docx` imported from
  `writer.py` — reuse the renderers, don't duplicate them
- Returns `{resume_path, cover_letter_path}` or `{}` based on flag

`should_submit`: routes on `state["errors"]` and `state["ats_field_map"]`.

All 15 Phase 4 tests green after this commit. **82 + 15 = 97/97.**

---

### Commit 7 — `feat(submitter): setup nodes + scan_form`

**File:** `src/pipeline/agents/submitter.py`

Implement `load_submission_job`, `detect_ats`, `scan_form`.

`load_submission_job`: same pattern as `load_job` in writer.py.

`detect_ats`: import from `pipeline.ats` — do not duplicate.

`scan_form`:
- Playwright: navigate to `apply_url`, snapshot, extract form HTML
- Calls `_has_file_input(html)` → sets `state["needs_file_upload"]`
- Calls `_map_form_fields(html, ats_type)` → sets `state["ats_field_map"]`
- Safe default if Playwright unavailable: `needs_file_upload=True`, `ats_field_map={}`

No new tests for Playwright nodes (same rationale as Phase 2).

---

### Commit 8 — `feat(submitter): fill_form + submission_gate`

**File:** `src/pipeline/agents/submitter.py`

Implement `fill_form` (Greenhouse strategy) and `submission_gate` interrupt.

`fill_form`:
- Iterates `state["ats_field_map"]`, fills each via Playwright `triple_click` + `type`
- If `needs_file_upload=True` and `resume_path` is not None: uses file_upload for
  resume and cover letter inputs
- If `needs_file_upload=False`: skips attachment step
- Workday detected: surface warning "Workday requires manual navigation.
  Open {apply_url} in Chrome, then press Enter." — same confirmed pattern from notes.

`submission_gate` interrupt data:
```python
interrupt({
    "form_summary": {
        "company": job.company,
        "title": job.title,
        "ats_type": state["ats_type"],
        "fields_filled": list(state["ats_field_map"].keys()),
        "file_uploaded": state["needs_file_upload"] and state.get("resume_path") is not None,
    },
    "message": "Review summary. Reply 'yes' to submit, anything else to abort.",
})
```

---

### Commit 9 — `feat(submitter): submit + capture + persist + CLI`

**File:** `src/pipeline/agents/submitter.py`, `src/pipeline/cli.py`

Implement `submit_form`, `capture_confirmation`, `abort_submission`,
`persist_submission`, and `_submit()` CLI command.

`submit_form`: Playwright click submit, wait for navigation, call `_parse_confirmation`.

`capture_confirmation`: Playwright screenshot → `{output_dir}/{job_id}_confirmation.png`.

`persist_submission`:
- INSERT into `submissions` table
- UPDATE `jobs`: `status = 'applied'`, write `resume_path` + `cover_letter_path`
- `followup_due_date = submitted_at + 7 days`

`cli.py _submit()`: same interrupt/resume loop as `_write()`. Detects
`submission_gate` interrupt by checking `"form_summary"` in interrupt data.

---

### Commit 10 — `docs: README Phase 4 update`

- Status: Phase 4 complete, 97/97 tests
- Phase table: Phase 4 ✅, Phase 5 🔜
- Project structure: submitter.py ✅
- Add Submitter section with topology diagram
- LangGraph patterns: add hard submission gate + conditional render entries

---

## Files created or modified in Phase 4

| File | Action |
|---|---|
| `migrations/002_content_json_columns.sql` | New |
| `src/pipeline/state.py` | Add `needs_file_upload`, `ats_field_map`, `submission_confirmed` |
| `src/pipeline/agents/writer.py` | Modify — strip rendering, update persist + review_interrupt |
| `src/pipeline/agents/submitter.py` | Full implementation |
| `src/pipeline/cli.py` | Add `_submit()` interrupt/resume loop |
| `tests/fixtures/greenhouse_form.html` | New |
| `tests/fixtures/greenhouse_form_no_upload.html` | New |
| `tests/fixtures/workday_form.html` | New |
| `tests/fixtures/greenhouse_confirmation.html` | New |
| `tests/fixtures/submission_error.html` | New |
| `tests/test_phase4.py` | New — 15 tests |
| `tests/test_phase3.py` | Modify — update `test_write_resume_calls_llm_once` assertion |
| `README.md` | Updated |

**Expected final test count: 97/97** (82 existing + 15 new)
