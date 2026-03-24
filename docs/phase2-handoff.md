# Phase 2 Handoff — Playwright Auth Sessions

**Date:** 2026-03-24  
**Branch:** main  
**Current state:** Phase 1 complete. 34/34 tests passing.  
`scrape_linkedin` and `scrape_ziprecruiter` are no-op stubs in `discoverer.py`.  
`scripts/save_auth.py` exists but is incomplete (cut off mid-function body).

---

## Goal

Replace the two scraper stubs with real Playwright implementations that load a saved
browser session (`storage_state`) instead of re-authenticating on every run. A one-shot
helper script handles the interactive login and saves the session file.

---

## Architecture decisions

| Decision | Choice | Reason |
|---|---|---|
| Browser channel | `channel="chrome"` | Better bot detection resistance vs bundled Chromium |
| Multi-platform login UX | One browser, one tab per platform | Cleaner than separate browser instances |
| Stale session handling | Detect login redirect → warn with re-run instructions → return `[]` | Actionable error over silent failure |
| LinkedIn result ceiling | Up to `max_results_per_source` | Sam has Premium; no paywall modal concern |
| Playwright platform scope | `sys_platform == 'win32'` only | No Linux x86_64 wheel for pinned version |
| Parser architecture | Pure functions extracted from scraper nodes | Enables unit testing without a live browser |

---

## New pure functions (fully unit testable)

```python
# In discoverer.py
def _get_auth_path(platform: str) -> Path
def _is_login_redirect(url: str, platform: str) -> bool
def _parse_linkedin_cards(html: str) -> list[RawJobListing]
def _parse_ziprecruiter_cards(html: str) -> list[RawJobListing]
```

Scraper nodes become thin wrappers: launch browser → get page HTML → call pure parser → return results.

---

## Test plan (15 tests, written before implementation)

**Auth helpers — 4 tests**
- `test_get_auth_path_linkedin` → returns `playwright/.auth/linkedin.json`
- `test_get_auth_path_ziprecruiter` → returns `playwright/.auth/ziprecruiter.json`
- `test_is_login_redirect_detects_linkedin_login` → URL contains `/login` → `True`
- `test_is_login_redirect_safe_url` → `/feed/` URL → `False`

**LinkedIn card parser — 4 tests**
- `test_parse_linkedin_cards_happy_path` → fixture HTML → correct `RawJobListing` fields
- `test_parse_linkedin_cards_empty_page` → no cards → `[]`
- `test_parse_linkedin_cards_missing_company` → malformed card skipped gracefully, no crash
- `test_parse_linkedin_cards_salary_extracted` → salary string → `compensation_low/high` populated

**ZipRecruiter card parser — 3 tests**
- `test_parse_ziprecruiter_cards_happy_path` → fixture HTML → correct `RawJobListing` fields
- `test_parse_ziprecruiter_cards_empty_page` → no cards → `[]`
- `test_parse_ziprecruiter_cards_salary_range` → `"$80K - $110K"` → compensation fields set

**Scraper node behavior — 4 tests (monkeypatched)**
- `test_scrape_linkedin_missing_auth_returns_empty`
- `test_scrape_ziprecruiter_missing_auth_returns_empty`
- `test_scrape_linkedin_stale_session_returns_empty_with_warning`
- `test_scrape_ziprecruiter_stale_session_returns_empty_with_warning`

**Fixture HTML files**
- `tests/fixtures/linkedin_job_cards.html` — minimal representative DOM (3 cards)
- `tests/fixtures/ziprecruiter_job_cards.html` — minimal representative DOM (3 cards)

---

## Commit plan

Work in strict TDD order: write all tests first (red), then implement to green, then commit.
Run tests after every commit using Docker:
```bash
cd /C/Users/sammo/Code/job-pipeline && .venv/bin/pytest tests/ -v
```

---

### Commit 1 — `build: make playwright dependency platform-conditional`

**File:** `pyproject.toml`

```toml
# Change:
"playwright>=1.47.0",
# To:
"playwright>=1.47.0; sys_platform == 'win32'",
```

**Why first:** Unblocks `uv sync` on Linux/Docker without `--no-install-package`.
No tests needed — verify with `uv sync` in Docker after the change.

---

### Commit 2 — `test(phase2): add fixture HTML for card parsers`

**New files:**
- `tests/fixtures/linkedin_job_cards.html`
- `tests/fixtures/ziprecruiter_job_cards.html`

Each contains 3 minimal job card divs using the known DOM structure.
No assertions yet — these are test data only.

**LinkedIn card structure (minimal):**
```html
<div class="job-search-card">
  <h3 class="base-search-card__title">Senior Rails Engineer</h3>
  <h4 class="base-search-card__subtitle">Acme Health</h4>
  <span class="job-search-card__location">Chicago, IL</span>
  <a class="base-card__full-link" href="/jobs/view/123456789"></a>
  <span class="job-search-card__salary-info">$140K/yr - $170K/yr</span>
</div>
```

**ZipRecruiter card structure (minimal):**
```html
<article class="job_result">
  <h2 class="job_title"><a href="/c/Acme/j/abc123">Backend Engineer</a></h2>
  <a class="company_name">TechCorp</a>
  <span class="location">Chicago, IL (Remote)</span>
  <span class="compensation">$120,000 - $150,000/yr</span>
</article>
```

---

### Commit 3 — `test(phase2): write failing tests for pure parsing functions`

**New file:** `tests/test_phase2.py`

Write all 15 tests listed in the test plan above. All red at this point.
Import the four pure functions that don't exist yet — collection will error.
That's expected and correct for TDD.

**Commit message:**
```
test(phase2): write failing tests for auth helpers and card parsers

15 tests covering _get_auth_path, _is_login_redirect,
_parse_linkedin_cards, _parse_ziprecruiter_cards, and scraper
node behavior for missing/stale auth. All red — implementation next.
```

---

### Commit 4 — `feat(discoverer): extract pure parsing functions and auth helpers`

**File:** `src/pipeline/agents/discoverer.py`

Add the four pure functions below the existing helpers:
- `_get_auth_path(platform)` — returns `Path` to auth file
- `_is_login_redirect(url, platform)` — checks if current URL is a login page
- `_parse_linkedin_cards(html)` — BS4 parse of LinkedIn job card HTML
- `_parse_ziprecruiter_cards(html)` — BS4 parse of ZipRecruiter job card HTML

Do **not** touch the scraper stubs yet. Just add the pure functions.
Auth helpers and card parser tests should go green. Scraper node tests still red.

**Commit message:**
```
feat(discoverer): extract pure parsing functions for LinkedIn and ZipRecruiter

_get_auth_path, _is_login_redirect, _parse_linkedin_cards,
_parse_ziprecruiter_cards — all pure, no browser dependency.
11/15 Phase 2 tests now passing.
```

---

### Commit 5 — `feat(discoverer): implement scrape_linkedin with storage_state auth`

**File:** `src/pipeline/agents/discoverer.py`

Replace the `scrape_linkedin` stub with full implementation:
1. `_get_auth_path("linkedin")` — check exists, warn + return `[]` if not
2. `async_playwright` → `launch(headless=True, channel="chrome")`
3. `new_context(storage_state=auth_path)`
4. Navigate to `linkedin.com/jobs/search/?keywords=...&location=...&sortBy=DD`
5. `_is_login_redirect(page.url, "linkedin")` → warn with re-run instructions + return `[]`
6. `page.content()` → `_parse_linkedin_cards(html)`
7. Return `{"raw_results": results}`

All 15 Phase 2 tests should pass after this commit.

**Commit message:**
```
feat(discoverer): implement scrape_linkedin with Playwright storage_state

Loads playwright/.auth/linkedin.json session. Detects missing auth
and stale session redirect with actionable re-run warning. Delegates
HTML parsing to pure _parse_linkedin_cards function.
```

---

### Commit 6 — `feat(discoverer): implement scrape_ziprecruiter with storage_state auth`

**File:** `src/pipeline/agents/discoverer.py`

Same pattern as Commit 5 for ZipRecruiter:
1. Check auth file, warn + return `[]` if missing
2. Launch Chrome with `storage_state=auth_path`
3. Navigate to `ziprecruiter.com/jobs-search?search=...&location=...`
4. Detect login redirect, warn + return `[]` if stale
5. `_parse_ziprecruiter_cards(page.content())`

**Commit message:**
```
feat(discoverer): implement scrape_ziprecruiter with Playwright storage_state

Same auth pattern as scrape_linkedin. All 15 Phase 2 tests passing.
49/49 total tests passing.
```

---

### Commit 7 — `feat(scripts): implement save_auth.py multi-tab login helper`

**File:** `scripts/save_auth.py`

Rewrite from the incomplete stub. Key behavior:
- `--platform linkedin|ziprecruiter|all`
- `--dry-run` flag: validates Chrome availability + auth dir writeability, no browser
- `all`: opens one Chrome window with one tab per platform
- Waits for user to log in to all tabs, then presses Enter once
- Saves each tab's `context.storage_state(path=auth_path)` to its file
- Prints confirmation with file path and approximate expiry reminder

No unit tests. Covered by `--dry-run` flag for CI/smoke purposes.

**Commit message:**
```
feat(scripts): implement save_auth.py with multi-tab Chrome login flow

Opens one headed Chrome window with a tab per platform (linkedin,
ziprecruiter, or both). Saves storage_state to playwright/.auth/.
--dry-run validates Chrome availability without opening a browser.
```

---

### Commit 8 — `docs: update README for Phase 2 completion`

**File:** `README.md`

- Phase table: mark Phase 2 ✅, Phase 3 🔜
- Add **Auth setup** section explaining `save_auth.py` usage
- Update test count (49 passing)
- Note `sys_platform == 'win32'` constraint for Playwright

**Commit message:**
```
docs: update README for Phase 2 completion

Phase table, auth setup instructions, updated test count (49/49).
```

---

## After all commits — verify end-to-end

```bash
# In Docker — all 49 tests green
cd /C/Users/sammo/Code/job-pipeline && .venv/bin/pytest tests/ -v

# On Windows — run a real discovery search using all four sources
pipeline discover --query "rails engineer" --location "Chicago, IL" --sources "indeed,dice,linkedin,ziprecruiter"
```

If LinkedIn or ZipRecruiter auth files are missing, the scraper warns and falls back
to Indeed + Dice only. No crash.

---

## Files created or modified in Phase 2

| File | Action |
|---|---|
| `pyproject.toml` | Modified — playwright made platform-conditional |
| `tests/fixtures/linkedin_job_cards.html` | New |
| `tests/fixtures/ziprecruiter_job_cards.html` | New |
| `tests/test_phase2.py` | New — 15 tests |
| `src/pipeline/agents/discoverer.py` | Modified — 4 new pure functions + 2 stub replacements |
| `scripts/save_auth.py` | Rewritten from incomplete stub |
| `README.md` | Modified |
