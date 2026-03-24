# job-pipeline

A LangGraph multi-agent job application pipeline. Four agents — Discoverer, Writer, Submitter, Tracker — coordinate to automate job search, document generation, form submission, and follow-up tracking.

**Status:** Phase 4 complete — Submitter agent fully implemented. ATS detection, conditional docx rendering, Playwright form fill, hard submission gate interrupt, screenshot receipting, and DB persistence. 97/97 tests passing.

---

## Architecture

```
CLI input ──► [discover] ──► [write] ──► [submit]
                  │                          │
                  └────────── [track] ◄───────┘
                                  │
                             SQLite DB
                    (jobs, submissions, search_runs,
                          company_cache)
```

Each agent is a compiled LangGraph subgraph. The top-level graph wires them together with a shared `AsyncSqliteSaver` checkpoint store — interrupted runs resume from the last completed node.

**LangGraph patterns exercised so far:**
- `Send` API — parallel fan-out across job sources (Discoverer, Phase 1)
- `Annotated[list, operator.add]` reducer — accumulates results from parallel branches
- `interrupt_before` + `Command(resume)` — human-in-the-loop triage gate (Phase 1)
- `AsyncSqliteSaver` — checkpoint store for resume-from-failure (Phase 0)
- `interrupt()` at multiple gates — pre-write interview + document review (Phase 3)
- **Cycle / revision loop** — `apply_feedback → write_resume` loops back until approved or max rounds hit (Phase 3)
- `add_conditional_edges` with routing function — `should_revise` branches to approve / revise / warn (Phase 3)
- **Conditional render node** — `render_documents_if_needed` scans form HTML before rendering; skips `.docx` creation entirely when no file upload input detected (Phase 4)
- **Two-stage conditional edges** — `should_submit` (post scan-form) and `should_submit_after_gate` (post interrupt) are separate routing functions with different semantics (Phase 4)
- **Hard interrupt gate** — `submission_gate` surfaces a form summary and requires explicit `'yes'` before any submit action; state is checkpointed at the interrupt boundary (Phase 4)


---

## Quickstart

**Requirements:** Python 3.11+, [uv](https://docs.astral.sh/uv/)

```bash
# 1. Clone and install
git clone https://github.com/montal95/job-pipeline
cd job-pipeline
uv sync

# 2. Configure
cp .env.example .env
# Edit .env — set ANTHROPIC_API_KEY and CV_PATH at minimum

# 3. Initialize the database
pipeline db migrate

# 4. (Optional) Save LinkedIn and ZipRecruiter auth sessions — Windows only
python scripts/save_auth.py --platform all
# This opens a Chrome window. Log in to each tab, then press Enter.
# Sessions are saved to playwright/.auth/ and last ~30 days.

# 5. Run a discovery search
pipeline discover --query "rails engineer" --location "Chicago, IL"
# Default sources: indeed,dice
# With auth:       --sources "indeed,dice,linkedin,ziprecruiter"
```

### Running tests

Tests run via the `.venv` Python directly. `uv run` re-resolves the lockfile and
fails on platforms where the `playwright` wheel isn't available (Linux x86_64 in CI/Docker).

```bash
# From the repo root — works on Linux/Docker and Windows
.venv/bin/pytest tests/ -v          # Linux / Docker
.venv\Scripts\pytest.exe tests\ -v  # Windows PowerShell (note the & prefix: & .\.venv\Scripts\pytest.exe)
```

**Current test count: 97 passing** (11 Phase 0 + 23 Phase 1 + 17 Phase 2 + 13 config CLI + 18 Phase 3 + 15 Phase 4)


---

## CLI Reference

```
pipeline discover [--query QUERY] [--location LOCATION] [--remote/--no-remote] [--sources SOURCES]
pipeline write <job_id>
pipeline submit <job_id>
pipeline track
pipeline run [--query QUERY] [--location LOCATION]
pipeline db migrate
```

`--sources` accepts `indeed`, `dice`, `linkedin`, `ziprecruiter` (comma-separated).  
LinkedIn and ZipRecruiter require a saved auth session — run `save_auth.py` first.  
If an auth file is missing or expired, the scraper warns and falls back gracefully.

---

## Project structure

```
src/pipeline/
  config.py          # Settings (pydantic-settings) + LLM_MODEL pin
  state.py           # PipelineState TypedDict + all Pydantic models
  database.py        # aiosqlite connection management + migration runner
  graph.py           # Top-level graph with AsyncSqliteSaver checkpointing
  cli.py             # typer CLI — all subcommands; interrupt/resume for discover
  agents/
    discoverer.py    # ✅ Phase 1+2: httpx scrapers, Playwright auth, Send fan-out, triage
    writer.py        # ✅ Phase 3: CV → tailored resume + cover letter (revision loop, 2 interrupt gates)
    submitter.py     # ✅ Phase 4: ATS form fill, conditional render, hard submission gate
    tracker.py       # 🔜 Phase 5: Status dashboard, follow-up scheduling
migrations/
  001_initial.sql    # jobs, submissions, search_runs, company_cache tables
  002_content_json_columns.sql  # resume_content_json + cover_letter_content_json columns
scripts/
  save_auth.py       # ✅ Phase 2: interactive Chrome login → saves playwright/.auth/
tests/
  fixtures/
    sample_jobs.py              # Synthetic cross-source fixtures (Phase 1)
    linkedin_job_cards.html     # Minimal LinkedIn card DOM (Phase 2)
    ziprecruiter_job_cards.html # Minimal ZipRecruiter card DOM (Phase 2)
    sample_cv.txt               # Synthetic CV text for Writer tests (Phase 3)
    sample_job.json             # Complete JobListing JSON fixture (Phase 3)
    sample_resume_llm_response.json       # Realistic LLM resume JSON (Phase 3)
    sample_cover_letter_llm_response.json # Realistic LLM cover letter JSON (Phase 3)
    greenhouse_form.html                  # Greenhouse apply form with file inputs (Phase 4)
    greenhouse_form_no_upload.html        # Greenhouse form without file inputs (Phase 4)
    workday_form.html                     # Workday DOM with aria-label fields (Phase 4)
    greenhouse_confirmation.html          # Post-submit success page (Phase 4)
    submission_error.html                 # Generic error page (Phase 4)
  test_phase0.py     # Graph compilation + state schema smoke tests (11 tests)
  test_phase1.py     # Discoverer unit tests — fingerprint, dedup, ATS, Send (23 tests)
  test_phase2.py     # Auth helpers, card parsers, scraper node behavior (17 tests)
  test_phase3.py     # Writer unit tests — CV loading, gap extraction, prompts, docx, nodes (18 tests)
  test_phase4.py     # Submitter unit tests — file detection, field mapping, render, routing (15 tests)
  test_config_cli.py # Config set/read helpers (13 tests)
docs/
  phase2-handoff.md  # Commit plan and architecture decisions for Phase 2
  phase3-handoff.md  # Commit plan and architecture decisions for Phase 3
```


---

## Phase plan

| Phase | Scope | Status |
|-------|-------|--------|
| 0 | Scaffolding, state schema, DB, stub graphs, CLI | ✅ Complete |
| 1 | Discoverer — httpx scrapers, Send API fan-out, triage interrupt, persist to DB | ✅ Complete |
| 2 | Playwright auth sessions (LinkedIn, ZipRecruiter), save_auth.py helper | ✅ Complete |
| 3 | Writer — LLM calls, python-docx rendering, revision loop | ✅ Complete |
| 4 | Submitter — ATS strategies, Playwright form fill, hard submission gate | ✅ Complete |
| 5 | Tracker — rich dashboard, follow-up scheduling | 🔜 Next |
| 6 | Polish, Mermaid architecture diagram, blog post | ⬜ |

---

## Discoverer — what Phase 1 built

The Discoverer agent runs all job sources in parallel using LangGraph's `Send` API,
merges and deduplicates results by fingerprint, cross-references the DB to suppress
already-seen listings, then pauses for user triage before persisting approved jobs.

```
parse_search_params
      │
fan_out_sources ──(Send)──► scrape_indeed       ──┐
                ──(Send)──► scrape_dice         ──┤
                ──(Send)──► scrape_linkedin*    ──┤──► merge_results
                ──(Send)──► scrape_ziprecruiter*──┘         │
                                                      triage_interrupt  ← user reviews shortlist
                                                            │
                                                       persist_to_db
```

\* Phase 2 stubs — require Playwright auth sessions.

**Dedup logic:** listings are fingerprinted by `sha256(company|title|location)[:16]`.
Cross-source duplicates (same role posted on Indeed and Dice) are collapsed to one entry.
Previously skipped, applied, or submitted listings are suppressed silently on re-discovery.

**Triage gate:** the graph checkpoints at `triage_interrupt`. The CLI renders a Rich table,
collects per-listing `a`/`s` decisions, then resumes with `Command(resume=decisions)`.
If the terminal is closed mid-triage, re-running the same `thread_id` restores state
from the checkpoint and re-presents the list.

## Writer — what Phase 3 built

The Writer agent takes a job DB record plus the user's CV and produces two `.docx` files:
a tailored resume and a cover letter. It exercises the most complex LangGraph topology in
the pipeline — two interrupt gates and a cycle.

```
load_job → fetch_cv → research_company → pre_write_interview (interrupt)
                                                   │
                                          write_resume (LLM)
                                                   │
                                        write_cover_letter (LLM)
                                                   │
                                          review_interrupt (interrupt)
                                                   │
                        ┌──────────────────────────┤
                   approved                   feedback given
                        │                          │
                persist_documents          apply_feedback (LLM)
                        │                          │
                       END              ────► write_resume (cycle)
                                         (max 3 rounds, then warn_and_exit)
```

**Key implementation decisions:**

**Structured JSON output, not free-form text.** The LLM returns a typed JSON object
(`ResumeContent` / `CoverLetterContent`) that is validated by Pydantic before being
passed to the deterministic `python-docx` renderer. This separates generation from
presentation — revision rounds only re-call the LLM, not the renderer.

**Two interrupt gates.** `pre_write_interview` surfaces gap keywords extracted from the
JD against the CV and asks targeted questions before generation begins. `review_interrupt`
shows the generated doc paths and waits for explicit approval, feedback text, or abort.
Both are proper LangGraph `interrupt()` calls — the state is checkpointed at the boundary,
so a closed terminal can resume the next day via `pipeline write <job_id>`.

**Revision cycle.** `should_revise` routes `review_interrupt` output to one of three
branches: `persist_documents` (approved), `apply_feedback` (feedback present + rounds
remain), or `warn_and_exit` (max rounds exceeded). The `apply_feedback → write_resume`
back-edge is the cycle. `apply_feedback` passes the previous resume JSON plus feedback
as a targeted revision prompt — cheaper and more accurate than full regeneration.

**Zero-token setup.** `load_job` reads from SQLite. `fetch_cv` reads from disk (cached
in state after first call). `research_company` checks `company_cache` first; falls back
to a DuckDuckGo Instant Answer API call and writes the result back to the cache.

**Hyperlinks in python-docx.** Like the npm `docx` library, `python-docx` doesn't
natively support hyperlink runs. The workaround uses OOXML relationship + element
construction directly via `docx.oxml`.

---

## Submitter — what Phase 4 built

The Submitter agent takes a job DB record with approved content JSON and handles
end-to-end ATS form submission. It introduces the most safety-critical LangGraph
pattern in the pipeline: a hard interrupt that prevents any form submission without
explicit user confirmation.

```
load_job → detect_ats → scan_form
                            │
              should_submit (errors? → abort_submission)
                            │
              render_documents_if_needed
              (only if ATS form has <input type="file">)
                            │
                       fill_form
                            │
                  submission_gate ← hard interrupt
                  (form summary displayed; requires 'yes')
                            │
         should_submit_after_gate (yes → submit_form, else → abort)
                            │
              submit_form → capture_confirmation → persist_submission → END
```

**Key implementation decisions:**

**Deferred docx rendering (Option B).** The Writer generates structured `ResumeContent`
and `CoverLetterContent` JSON and persists it to `resume_content_json` /
`cover_letter_content_json` DB columns. The Submitter's `render_documents_if_needed`
node reads those columns and renders `.docx` files only if `scan_form` detected a file
upload input on the ATS form. If the form has no file input, no files are created at all.
This eliminates unnecessary disk I/O for ATS platforms that parse resumes from pasted
text or structured fields.

**BS4 label parsing gotcha.** `soup.find_all("label", for_=True)` returns nothing with
`html.parser` because `for` is a reserved Python keyword and BeautifulSoup's keyword
argument normalization silently drops it. The fix is `attrs={"for": True}`. Worth
knowing before building any form scraper.

**Workday fast-path.** Workday drops WebSocket connections on programmatic navigation.
`scan_form` detects Workday via `ats_type` and immediately returns an empty field map
with a warning, surfacing the apply URL for manual browser handling. `fill_form`
echoes a secondary warning. No Playwright session is opened for Workday in v1.

**Two routing functions, different semantics.** `should_submit` routes post-`scan_form`
on errors + field map (did scanning succeed?). `should_submit_after_gate` routes post-
`submission_gate` on `human_approved` (did the user say yes?). Keeping them separate
makes the graph topology self-documenting.

**Browser session doesn't survive the interrupt.** LangGraph `interrupt()` checkpoints
state and exits the process. The Playwright browser opened by `fill_form` is gone by
the time `submit_form` runs. `submit_form` therefore re-navigates and re-fills before
clicking submit — the re-fill is not a bug, it's necessary.

---

## Token minimization strategy

LLM tokens are reserved for judgment tasks only:

| Step | Approach | LLM? |
|------|----------|-------|
| Job listing fetch | httpx scraper | ❌ |
| JD field parsing | regex + BS4 | ❌ |
| Company research | httpx + search scraper, cached in DB | ❌ |
| Dedup | fingerprint hash | ❌ |
| ATS fingerprinting | URL pattern matching | ❌ |
| Form filling | Playwright + field maps | ❌ |
| Document rendering | python-docx from structured JSON | ❌ |
| **Fit assessment** | **one-sentence judgment** | ✅ optional |
| **Resume tailoring** | **structured JSON output** | ✅ |
| **Cover letter** | **full text generation** | ✅ |

Target: 2–3 LLM calls per application.
