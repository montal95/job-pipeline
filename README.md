# job-pipeline

A LangGraph multi-agent job application pipeline. Four agents — Discoverer, Writer, Submitter, Tracker — coordinate to automate job search, document generation, form submission, and follow-up tracking.

**Status:** Phase 0 — scaffolding complete. Agents are stubbed and wired; implementation begins in Phase 1.

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

**Key LangGraph patterns used:**
- `Send` API — parallel fan-out across job sources (Discoverer)
- `interrupt()` — human-in-the-loop gates at triage, pre-write, review, and submission
- Cycles — revision loop in the Writer (write → review → revise → write)
- `AsyncSqliteSaver` — checkpoint store for resume-from-failure

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
uv run pipeline db migrate

# 4. Run the tests (Phase 0: all graph compilation smoke tests)
uv run pytest

# 5. Try the CLI (Phase 0: stubs — no real output yet)
uv run pipeline discover --query "rails engineer" --location "Chicago, IL"
uv run pipeline track
```

---

## CLI Reference

```
pipeline discover [--query QUERY] [--location LOCATION] [--remote/--no-remote]
pipeline write <job_id>
pipeline submit <job_id>
pipeline track
pipeline run [--query QUERY] [--location LOCATION]
pipeline db migrate
```

---

## Project structure

```
src/pipeline/
  config.py          # Settings (pydantic-settings) + LLM_MODEL pin
  state.py           # PipelineState TypedDict + all Pydantic models
  database.py        # aiosqlite connection management + migration runner
  graph.py           # Top-level graph with AsyncSqliteSaver checkpointing
  cli.py             # typer CLI — all subcommands
  agents/
    discoverer.py    # Search, dedup, triage interrupt
    writer.py        # CV → tailored resume + cover letter (with revision loop)
    submitter.py     # ATS form fill + hard submission gate
    tracker.py       # Status dashboard, follow-up scheduling
migrations/
  001_initial.sql    # jobs, submissions, search_runs, company_cache tables
tests/
  test_phase0.py     # Graph compilation + state schema smoke tests
```

---

## Phase plan

| Phase | Scope | Status |
|-------|-------|--------|
| 0 | Scaffolding, state schema, DB, stub graphs, CLI | ✅ Complete |
| 1 | Discoverer — httpx scrapers, Send API fan-out, triage interrupt | 🔜 Next |
| 2 | Playwright auth sessions (LinkedIn, ZipRecruiter) | ⬜ |
| 3 | Writer — LLM calls, python-docx rendering, revision loop | ⬜ |
| 4 | Submitter — ATS strategies, Playwright form fill | ⬜ |
| 5 | Tracker — rich dashboard, follow-up scheduling | ⬜ |
| 6 | Polish, README architecture diagram, blog post | ⬜ |

---

## Token minimization strategy

LLM tokens are reserved for judgment tasks only:
- ✅ Resume tailoring (LLM)
- ✅ Cover letter writing (LLM)  
- ✅ Fit assessment — optional (LLM)
- ❌ Job listing fetch → httpx scraper
- ❌ JD field parsing → regex + BS4
- ❌ Company research → httpx + search scraper, cached
- ❌ Dedup → fingerprint hash
- ❌ Document rendering → python-docx from structured JSON
- ❌ ATS fingerprinting → URL pattern matching
- ❌ Form filling → Playwright + field maps

Target: 2–3 LLM calls per application.
