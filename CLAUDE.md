# CLAUDE.md — job-pipeline

Context for Claude Code sessions working in this repo. Read this once at
the start of every session. Keep it short; if something belongs in a
playbook, it goes in `CONTRIBUTING.md` instead.

## Orientation

- Python 3.12 · LangGraph · typer · pydantic-settings · httpx · Playwright
- Public reference implementation — legibility matters as much as working code.
- For **TDD rules, test conventions, commit format, and the playbooks for
  adding a new job source or ATS strategy**, read `CONTRIBUTING.md`. Do
  not duplicate that content here.

## Repo layout (read before touching code)

| Path | Role |
|------|------|
| `src/pipeline/agents/discoverer.py` | Scrapers, `merge_results`, `fan_out_sources`, `SOURCE_NODE_MAP` |
| `src/pipeline/state.py`             | `PipelineState` TypedDict, `SearchParams`, enums |
| `src/pipeline/config.py`            | pydantic-settings `Settings` class |
| `src/pipeline/cli.py`               | typer CLI (`discover`, `track`, …) |
| `src/pipeline/database.py`          | `get_connection()` async context manager |
| `migrations/`                       | Numbered SQL migration files |
| `tests/test_discoverer.py`          | Discoverer unit tests |
| `tests/conftest.py`                 | Shared fixtures: `sample_job`, `bare_db`, `seeded_db` |
| `tests/fixtures/`                   | HTML/JSON fixtures for scraper tests |

## Patterns to follow (non-negotiable)

### Code style
- Pure functions named `_transform_*` / `_parse_*` — no browser, no DB,
  fully unit-testable. Every node should have at least one pure helper.
- Scraper nodes return `{"raw_results": list[RawJobListing]}` — the
  `operator.add` reducer on `PipelineState["raw_results"]` handles fan-out.
- All DB access via `async with get_connection() as conn:`.
- Rich output: `console.print(markup=False)` for any dynamic content.
- Never raise from a scraper — catch, print a yellow warning, return empty list.

### httpx (for JSON/API sources)
- Always `async with httpx.AsyncClient(headers=HEADERS, timeout=15) as client:`.
- Reuse the module-level `HEADERS` constant (don't redefine per call).
- Exceptions handled per the scraper rule above — no bare `raise`.

### typer CLI
- Prefer `--flag/--no-flag` over bare `--flag` for booleans.
- Every `Option` and `Argument` takes a `help=` string — the CLI is the
  primary user surface.
- Ordering inside a command signature: positional `Argument`s first,
  then `Option`s.

### Migrations
- File naming: `NNN_snake_case.sql` (zero-padded 3-digit prefix).
- Next number = `ls migrations/ | tail -1` + 1 — never skip, never renumber.
- One logical change per file.

## Running the test suite

```bash
# Linux / Docker (CI parity, preferred)
docker run --rm -v "$(pwd)":/repo python:3.12-slim sh /repo/run_tests.sh

# Windows / uv venv (fast iteration)
.venv\Scripts\pytest.exe tests\ -v
```

Target: <2s full run. Every commit leaves green.

## Never commit

- `.env` (use `.env.example` as template)
- `config/companies.yml` (private watchlist)
- `playwright/.auth/*.json`
- Anything under `data/` or `output/`
