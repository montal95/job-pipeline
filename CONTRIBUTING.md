# Contributing to job-pipeline

This project is a public reference implementation — clean architecture and
legible patterns are as important as working code. This guide explains how
to extend each part of the pipeline without breaking existing agents.

---

## Development setup

```bash
git clone https://github.com/montal95/job-pipeline
cd job-pipeline
uv sync
cp .env.example .env
# Edit .env — set ANTHROPIC_API_KEY and CV_PATH at minimum
pipeline db migrate
```

**Running tests:**
```bash
# Linux / Docker (used in CI)
docker run --rm -v $(pwd):/repo python:3.12-slim sh /repo/run_tests.sh

# Windows (uv venv)
.venv\Scripts\pytest.exe tests\ -v
```

All tests must pass before opening a PR. The suite is fast (< 2s) — run it
after every change.

---

## Adding a new job source

Each job source is a single async scraper node in `discoverer.py`.
Here's the minimal pattern:

**1. Write the scraper node:**

```python
async def scrape_myboard(state: PipelineState) -> dict:
    """Scrape MyBoard via httpx + BS4. Gracefully handles errors."""
    params = state["search_params"]
    results: list[RawJobListing] = []
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=15) as client:
            resp = await client.get(f"https://myboard.com/jobs?q={params.query}")
        # parse resp.text into RawJobListing objects
        ...
    except Exception as exc:
        console.print(f"[yellow]⚠ MyBoard error: {exc}[/yellow]")
    return {"raw_results": results}
```

Key rules:
- Return `{"raw_results": list[RawJobListing]}` — the `operator.add` reducer
  accumulates results from all parallel branches automatically.
- Never raise exceptions — catch and warn, return `{"raw_results": []}`.
- `state["search_params"]` has `query`, `location`, `remote`, `max_results_per_source`.

**2. Register the node in `SOURCE_NODE_MAP` and the graph:**

```python
# discoverer.py
SOURCE_NODE_MAP = {
    "indeed": "scrape_indeed",
    "dice": "scrape_dice",
    "myboard": "scrape_myboard",  # add here
    ...
}

# in build_discoverer_graph():
graph.add_node("scrape_myboard", scrape_myboard)
graph.add_edge("scrape_myboard", "merge_results")
```

**3. Add it to `.env.example` and `config.py`:**

```
ENABLED_SOURCES=indeed,dice,myboard,linkedin,ziprecruiter
```

**4. Write a fixture and tests:**

Add a minimal HTML fixture at `tests/fixtures/myboard_job_cards.html`
and add parser + scraper tests to `tests/test_discoverer.py`, following
the existing LinkedIn/ZipRecruiter pattern.

---

## Adding a new ATS strategy

ATS strategies live in `submitter.py` inside `_map_form_fields` and `fill_form`.
Each strategy is identified by `AtsType` enum value.

**1. Add the ATS pattern to `ats.py`:**

```python
ATS_PATTERNS: dict[str, list[str]] = {
    ...
    "myats": ["myats.com", "jobs.myats.io"],
}
```

**2. Add the `AtsType` enum value to `state.py`:**

```python
class AtsType(str, Enum):
    ...
    MYATS = "myats"
```

**3. Add a branch to `_map_form_fields`:**

```python
elif ats_type == AtsType.MYATS:
    for elem in soup.find_all("input", attrs={"data-field": True}):
        label = elem.get("data-field", "").strip()
        if label:
            result[label] = f"[data-field='{label}']"
```

**4. Handle the new ATS in `fill_form` if it needs special interaction.**

**5. Add HTML fixture + tests to `tests/test_submitter.py`.**

---

## Adding a new state field

All state lives in `PipelineState` (a `TypedDict`) in `state.py`.

```python
class PipelineState(TypedDict):
    ...
    my_new_field: str | None   # add with a comment explaining purpose
```

Also add a zero-value in `empty_state()`:

```python
def empty_state() -> PipelineState:
    return PipelineState(
        ...
        my_new_field=None,
    )
```

Add a test to `test_state.py` asserting the new field is present and
correctly typed in `empty_state()`.

---

## Test conventions

- **TDD:** write tests red-first, implement to make them green.
- **Pure functions first:** every node should have at least one pure helper
  function that is unit-tested without DB or Playwright.
- **No live I/O in tests:** mock the Anthropic API, use `tmp_path` for DB,
  never hit real job boards.
- **Fixtures in `conftest.py`** if the same setup is needed across multiple
  test files. File-local fixtures if used by only one file.
- **Playwright nodes are not unit-tested** — test the pure parsers they call,
  not the browser interaction itself.
- **Naming:** `test_{what}_{condition}` — e.g. `test_has_file_input_detects_hidden_input`.

---

## Commit conventions

This project uses [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(agent): description       # new capability
fix(agent): description        # bug fix
refactor(scope): description   # no behavior change
test(scope): description       # test additions or changes
docs: description              # README, CONTRIBUTING, handoff docs
db: description                # migration files
```

Each commit should leave the test suite green. The handoff docs in `docs/`
describe the exact commit sequence used for each phase — follow the same
pattern when adding new phases.
