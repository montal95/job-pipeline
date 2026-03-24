# job-pipeline

A LangGraph multi-agent job application pipeline. Four agents — Discoverer, Writer, Submitter, Tracker — coordinate to automate job search, document generation, form submission, and follow-up tracking.

**Status:** Phase 2 complete — All four job sources active. LinkedIn and ZipRecruiter scrapers use Playwright `storage_state` auth. Stale session detection with actionable re-run instructions. 51/51 tests passing.

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

**Coming next:**
- Writer agent — LLM calls, python-docx rendering, revision loop (Phase 3)


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

**Current test count: 51 passing** (11 Phase 0 + 23 Phase 1 + 17 Phase 2)


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
    writer.py        # 🔜 Phase 3: CV → tailored resume + cover letter (revision loop)
    submitter.py     # 🔜 Phase 4: ATS form fill + hard submission gate
    tracker.py       # 🔜 Phase 5: Status dashboard, follow-up scheduling
migrations/
  001_initial.sql    # jobs, submissions, search_runs, company_cache tables
scripts/
  save_auth.py       # ✅ Phase 2: interactive Chrome login → saves playwright/.auth/
tests/
  fixtures/
    sample_jobs.py              # Synthetic cross-source fixtures (Phase 1)
    linkedin_job_cards.html     # Minimal LinkedIn card DOM (Phase 2)
    ziprecruiter_job_cards.html # Minimal ZipRecruiter card DOM (Phase 2)
  test_phase0.py     # Graph compilation + state schema smoke tests (11 tests)
  test_phase1.py     # Discoverer unit tests — fingerprint, dedup, ATS, Send (23 tests)
  test_phase2.py     # Auth helpers, card parsers, scraper node behavior (17 tests)
docs/
  phase2-handoff.md  # Commit plan and architecture decisions for Phase 2
```


---

## Phase plan

| Phase | Scope | Status |
|-------|-------|--------|
| 0 | Scaffolding, state schema, DB, stub graphs, CLI | ✅ Complete |
| 1 | Discoverer — httpx scrapers, Send API fan-out, triage interrupt, persist to DB | ✅ Complete |
| 2 | Playwright auth sessions (LinkedIn, ZipRecruiter), save_auth.py helper | ✅ Complete |
| 3 | Writer — LLM calls, python-docx rendering, revision loop | 🔜 Next |
| 4 | Submitter — ATS strategies, Playwright form fill | ⬜ |
| 5 | Tracker — rich dashboard, follow-up scheduling | ⬜ |
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
