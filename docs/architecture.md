# job-pipeline — Architecture & Implementation Reference

This document consolidates design decisions, agent topologies, implementation
notes, and operational details. The README covers the quick-start path; this
document covers everything else.

---

## Table of contents

1. [Agent architecture](#1-agent-architecture)
2. [Discoverer](#2-discoverer)
3. [Writer](#3-writer)
4. [Submitter](#4-submitter)
5. [Tracker](#5-tracker)
6. [Job sources](#6-job-sources)
7. [LangGraph patterns reference](#7-langgraph-patterns-reference)
8. [Token minimization strategy](#8-token-minimization-strategy)
9. [Testing approach](#9-testing-approach)
10. [Local test walkthrough](#10-local-test-walkthrough)

---

## 1. Agent architecture

Four LangGraph subgraphs in sequence. Each agent is independently enterable
from the CLI — you can run the Writer on a specific job without re-running
discovery.

```
parse_search_params
      │
fan_out_sources ──(Send)──► scrape_*  (parallel)
                                │
                          merge_results
                                │
                        triage_interrupt ⚡
                                │
                          persist_to_db
```

```
load_job → fetch_cv → research_company → pre_write_interview ⚡
                                                │
                                         write_resume (LLM)
                                                │
                                       write_cover_letter (LLM)
                                                │
                                        review_interrupt ⚡
                                                │
                       ┌────────────────────────┤
                  approved                 feedback
                       │                       │
               persist_documents       apply_feedback (LLM)
                                               │
                                    ──► write_resume (cycle, max 3)
```

```
load_job → detect_ats → scan_form → render_docs_if_needed
                                          │
                                      fill_form
                                          │
                               submission_gate ⚡ (hard gate)
                                          │
                              submit → capture → persist
```

```
load_pipeline → schedule_followup → flag_overdue → render_dashboard
                                                         │
                                                  update_status
```

**⚡ interrupt** = LangGraph `interrupt()` — state checkpointed, terminal waits,
resumes via `Command(resume=...)`. Safe to close terminal and resume the next day.

---

## 2. Discoverer

### Topology

All sources run concurrently via `Send` API. Results are merged, deduplicated
by fingerprint, cross-referenced against the DB to suppress already-seen listings,
then paused for manual triage before persisting.

**Dedup:** `sha256(company|title|location)[:16]`. Cross-source duplicates collapse
to one entry. Previously skipped/applied/submitted listings suppress silently.

**Triage gate:** CLI renders Rich table, collects `a`/`s` per listing, resumes
with `Command(resume=decisions)`. Checkpoint survives terminal close.

### Pure transform functions (unit testable)

Each scraper delegates card parsing to a pure function that takes the raw
JS-extracted dict list and returns `list[RawJobListing]`:

| Source | Transform function | Fixture type |
|--------|--------------------|-------------|
| LinkedIn | `_parse_linkedin_cards(html)` | HTML file |
| Dice | `_transform_dice_jobs(jobs_data)` | inline dict |
| ZipRecruiter | `_transform_ziprecruiter_jobs(jobs_data)` | inline dict |
| Built In | `_transform_builtin_jobs(jobs_data)` | inline dict |
| Wellfound | `_transform_wellfound_jobs(jobs_data)` | inline dict (parked) |

### Salary extraction

`_parse_compensation(s)` handles `$120K-$150K/yr`, `$120,000-$150,000`, `$60/hr`.
`_extract_salary_from_description(text)` scans freeform card text for salary context
keywords and patterns — used as a fallback for sources that embed salary in
description snippets rather than dedicated fields.

### Location normalization

Vague country-level values (`USA`, `US`, `United States`, `2 Locations`) normalize
to blank. Specific city/state strings (`Chicago, IL, USA`) are preserved as-is.
Workplace type is extracted from location strings using `·` separators (ZipRecruiter)
or dedicated `workplace` fields (Built In, Wellfound).


---

## 3. Writer

### Key decisions

**Structured JSON output.** The LLM returns a typed JSON object (`ResumeContent` /
`CoverLetterContent`) validated by Pydantic before being passed to the deterministic
`python-docx` renderer. This separates generation from presentation — revision rounds
only re-call the LLM, not the renderer.

**Deferred rendering.** Writer persists content JSON to DB columns
(`resume_content_json`, `cover_letter_content_json`). The Submitter renders `.docx`
only if the ATS form has a file upload input. If no file input is detected, no files
are created — eliminates unnecessary disk I/O for ATS platforms that parse from
structured fields.

**Revision cycle.** `should_revise` routes `review_interrupt` output:
- `persist_documents` — approved
- `apply_feedback → write_resume` — feedback + rounds remaining (back-edge = cycle)
- `warn_and_exit` — max rounds exceeded (default 3)

`apply_feedback` passes previous resume JSON + feedback as a targeted revision
prompt — cheaper and more accurate than full regeneration.

**Two interrupt gates.**
- `pre_write_interview` — surfaces JD gap keywords, asks targeted questions before
  generation begins
- `review_interrupt` — shows doc paths, waits for approve / feedback / abort

**Hyperlinks in python-docx.** Neither `python-docx` nor the npm `docx` library
support hyperlink runs natively. Workaround: OOXML relationship + element construction
via `docx.oxml` directly.

**Zero-token setup nodes.** `load_job` reads from SQLite. `fetch_cv` reads from disk
(cached in state). `research_company` checks `company_cache` first; falls back to a
web search and writes result back to cache.

---

## 4. Submitter

### Key decisions

**Conditional render.** `scan_form` sets `needs_file_upload: bool`. If `False`,
`render_documents_if_needed` is a no-op — no `.docx` files created.

**Hard interrupt gate.** `submission_gate` surfaces a form summary (company, role,
ATS type, fields filled, file attached) and requires explicit `'yes'`. Nothing touches
the submit button without this. State is checkpointed at the boundary.

**Two routing functions, different semantics.**
- `should_submit` — post `scan_form`: did scanning succeed?
- `should_submit_after_gate` — post `submission_gate`: did user say yes?

**Browser session doesn't survive the interrupt.** `interrupt()` checkpoints state
and exits the process. The Playwright browser from `fill_form` is gone by the time
`submit_form` runs. `submit_form` re-navigates and re-fills before clicking submit —
this is required behavior, not a workaround.

**Workday fast-path.** Workday drops WebSocket connections on programmatic navigation.
`scan_form` detects Workday via `ats_type` and immediately returns an empty field map
with a warning, surfacing the apply URL for manual browser handling. v1 limitation.

**BS4 label parsing gotcha.** `soup.find_all("label", for_=True)` silently returns
nothing with `html.parser` because `for` is a reserved Python keyword. Fix:
`soup.find_all("label", attrs={"for": True})`.

### ATS support status

| ATS | v1 status |
|-----|-----------|
| Greenhouse | Full automation |
| Workday | Manual fallback — surfaces URL for user |
| Ashby | Stub |
| LinkedIn Easy Apply | Stub |
| Unknown | Warn + surface URL |

---

## 5. Tracker

### Key decisions

**Node order is the specification.** `schedule_followup` before `flag_overdue` before
`render_dashboard` — follow-up dates must be written before the overdue check reads
them, and the overdue list must exist before the dashboard renders the ⚠ indicator.

**LangGraph without LLMs.** Tracker uses LangGraph to sequence purely synchronous
SQLite operations with no async I/O or AI calls. Demonstrates the pattern is useful
as a workflow orchestrator beyond AI use cases.

**Pure functions drive testable logic.** `_count_by_status`, `_find_overdue`,
`_format_days_since` are all pure Python — no DB or terminal dependencies. Dashboard
rendering and DB writes are not unit-tested (integration concerns, same rationale
as Playwright nodes).

---

## 6. Job sources

| Source | Status | Method | Notes |
|--------|--------|--------|-------|
| LinkedIn | ✅ Working | Playwright + auth session | Requires `save_auth.py`; `headless=False` |
| Dice | ✅ Working | Playwright + JS extraction | No auth; public search |
| ZipRecruiter | ✅ Working | Playwright + JS extraction | No auth for discovery; auth needed for submitter |
| Built In Chicago | ✅ Working | Playwright + JS extraction | `builtin.com/jobs/chicago`; no auth |
| Wellfound | 🅿️ Parked | — | IP-based bot detection blocks Playwright; grep `WELLFOUND_PARKED` |
| Indeed | 🅿️ Parked | — | CAPTCHA blocks Playwright; grep `INDEED_PARKED` |

### Wellfound workarounds to research
1. Residential proxy rotation
2. `playwright-extra` + `puppeteer-extra-plugin-stealth`
3. Official Wellfound API (requires partnership application)

Card structure confirmed via DevTools (March 2026):
- Company cards: `.mb-6.w-full.rounded.border.border-gray-400.bg-white`
- Job divs: `.min-h-[50px]` (one per role)
- Lines: `[title, employment_type, salary?, location?, exp?, date, Save, Apply]`
- Apply link: `a[href*="/jobs/"]` → `wellfound.com/jobs/{id}-{slug}`


---

## 7. LangGraph patterns reference

| Pattern | Where used | Notes |
|---------|-----------|-------|
| `Send` API — parallel fan-out | Discoverer | One Send per source; `operator.add` reducer accumulates `raw_results` |
| `Annotated[list, operator.add]` reducer | Discoverer | Merges parallel branch outputs automatically |
| `interrupt_before` + `Command(resume)` | Discoverer triage | CLI loop pattern |
| `interrupt()` + `Command(resume)` | Writer (×2), Submitter | Checkpoints state; terminal-safe |
| `AsyncSqliteSaver` checkpoint store | All agents | `thread_id` scoped; resume-from-failure |
| Cycle / back-edge | Writer revision loop | `apply_feedback → write_resume`; bounded by `revision_round` counter |
| `add_conditional_edges` routing function | Writer, Submitter | `should_revise`, `should_submit`, `should_submit_after_gate` |
| Conditional render node | Submitter | Scans form before creating files |
| Hard interrupt gate | Submitter `submission_gate` | Requires explicit `'yes'`; no submit without it |
| Graph as DB workflow orchestrator | Tracker | No LLMs; pure SQLite + Rich |

---

## 8. Token minimization strategy

LLM tokens are reserved for judgment tasks only. Target: 2–3 LLM calls per application.

| Step | Approach | LLM? |
|------|----------|-------|
| Job listing fetch | Playwright JS extraction | ❌ |
| JD field parsing | regex + positional extraction | ❌ |
| Company research | httpx + search, cached in DB | ❌ |
| Dedup | fingerprint hash | ❌ |
| ATS fingerprinting | URL pattern matching | ❌ |
| Form filling | Playwright + field maps | ❌ |
| Document rendering | python-docx from structured JSON | ❌ |
| **Fit assessment** | **one-sentence judgment** | ✅ optional |
| **Resume tailoring** | **structured JSON output** | ✅ |
| **Cover letter** | **full generation** | ✅ |

---

## 9. Testing approach

Tests are domain-named and co-located with the code they cover. All 147 tests
run without a live browser, live API, or running DB — monkeypatching and fixture
states cover all Playwright and LLM paths.

| File | Scope | Count |
|------|-------|-------|
| `test_smoke.py` | Graph compile checks — all four agents | 4 |
| `test_state.py` | PipelineState schema, enums, `empty_state()` | 7 |
| `test_ats.py` | `detect_ats()` URL fingerprinting | 6 |
| `test_config.py` | `.env` read/write helpers | 13 |
| `test_database.py` | `get_connection()` context manager | 5 |
| `test_discoverer.py` | Fingerprint, compensation, merge, card parsers, auth, scrapers | 58 |
| `test_writer.py` | CV loading, gap extraction, prompts, parsers, rendering, nodes | 19 |
| `test_submitter.py` | File input, field mapping, render, confirmation, routing | 13 |
| `test_tracker.py` | Status counting, overdue, days-since, load, update | 9 |

**Running tests:**
```powershell
# Windows — from repo root
& .\.venv\Scripts\python.exe -m pytest tests/ -q
```

**Fixture conventions:**
- HTML files (`tests/fixtures/*.html`) document confirmed live DOM structure.
  They are loaded by tests that use BS4 (LinkedIn). For Playwright JS extraction
  sources (Dice, ZipRecruiter, Built In, Wellfound), inline Python dict fixtures
  match the shape returned by `page.evaluate()`.
- JSON files (`tests/fixtures/*.json`) hold realistic LLM response payloads.
- `.py` fixture files hold synthetic job and submission data collections.

---

## 10. Local test walkthrough

### Prerequisites

| Item | Where |
|------|-------|
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys |
| Absolute path to CV PDF | e.g. `C:\Users\sammo\Downloads\resume.pdf` |

### Install and configure

```powershell
cd C:\Users\sammo\Code\job-pipeline
winget install astral-sh.uv   # if not present
uv sync
& .\.venv\Scripts\pipeline.exe config set ANTHROPIC_API_KEY sk-ant-YOUR-KEY
& .\.venv\Scripts\pipeline.exe config set CV_PATH "C:\path\to\resume.pdf"
& .\.venv\Scripts\pipeline.exe db migrate
```

### LinkedIn auth (one-time)

```powershell
python scripts\save_auth.py --platform linkedin
```

A Chrome window opens. Log in, then press Enter. Session saved to
`playwright/.auth/linkedin.json` — lasts ~30 days.

### Discovery

```powershell
& .\.venv\Scripts\pipeline.exe discover `
  --query "software engineer rails" `
  --location "Chicago, IL" `
  --sources "linkedin,dice,ziprecruiter,builtin"
```

At the triage prompt: `a` to approve, Enter to skip. Approved jobs saved at `status=queued`.

### Write documents (per job)

```powershell
& .\.venv\Scripts\pipeline.exe write JOB_ID
```

Answer gap questions, then type `approve`, `abort`, or feedback to revise (max 3 rounds).

### Submit (Greenhouse ATS)

```powershell
& .\.venv\Scripts\pipeline.exe submit JOB_ID
```

Form fills automatically. **Type `yes` at the submission gate to submit** — anything
else aborts safely. Confirmation screenshot saved to `.\output\`.

### Dashboard

```powershell
& .\.venv\Scripts\pipeline.exe track
```

### Full pipeline shortcut

```powershell
& .\.venv\Scripts\pipeline.exe run --query "software engineer rails" --location "Chicago, IL"
```

### Troubleshooting

| Symptom | Fix |
|---------|-----|
| LinkedIn stale session warning | `python scripts\save_auth.py --platform linkedin` |
| `write` hangs at `research_company` | DuckDuckGo timeout — auto-recovers |
| Workday job hits manual fallback | Open the apply URL in Chrome manually |
| ZipRecruiter 0 results | Retry — occasional rate limiting on public search |
| `uv: command not found` | Re-run `winget install astral-sh.uv`, open new PowerShell |

### Useful DB queries

```powershell
# All jobs
sqlite3 .\data\test_pipeline.db "SELECT id, title, company, status FROM jobs ORDER BY discovered_at DESC"

# Queued (ready to write)
sqlite3 .\data\test_pipeline.db "SELECT id, title, company FROM jobs WHERE status='queued'"

# Applied + follow-up dates
sqlite3 .\data\test_pipeline.db "SELECT j.title, j.company, s.followup_due_date FROM jobs j JOIN submissions s ON s.job_id = j.id WHERE j.status='applied'"
```
