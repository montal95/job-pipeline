# Phase 3 Handoff — Writer Agent

**Date:** 2026-03-24
**Branch:** main
**Test state:** 51/51 passing (Phase 0–2 complete)
**Next phase:** Writer agent — LLM calls, python-docx rendering, revision loop

---

## Current repo state

```
src/pipeline/agents/
  discoverer.py   ✅ Full implementation (Phases 1+2)
  writer.py       🔜 Phase 0 stubs — all nodes defined, graph wired, no implementation
  submitter.py    🔜 Phase 0 stubs
  tracker.py      🔜 Phase 0 stubs
scripts/
  save_auth.py    ✅ Multi-tab Chrome login helper
docs/
  phase2-handoff.md   ← previous handoff
  phase3-handoff.md   ← this file
```

Run tests with:
```bash
cd /C/Users/sammo/Code/job-pipeline && .venv/bin/pytest tests/ -v
```

---

## What Phase 3 builds

The Writer agent takes a job DB record + the user's CV and produces two `.docx` files:
a tailored resume and a cover letter. It has two human-in-the-loop gates and a
revision cycle — the most complex LangGraph topology in the pipeline.

```
load_job → fetch_cv → research_company → pre_write_interview (interrupt)
    │
    ▼
write_resume (LLM) → write_cover_letter (LLM)
    │
    ▼
render_resume_docx → render_cover_letter_docx
    │
    ▼
review_interrupt (interrupt)
    │
    ├─ approved ──────────────────────────► persist_documents → END
    ├─ feedback + rounds remaining ──────► apply_feedback (LLM) → write_resume (cycle)
    └─ max rounds exceeded ──────────────► warn_and_exit → END
```

**LangGraph patterns exercised:**
- `interrupt()` at two gates: `pre_write_interview` and `review_interrupt`
- **Cycle:** `apply_feedback → write_resume` loops back until approved or max rounds hit
- `add_conditional_edges` with `should_revise` routing function (already stubbed)
- `revision_round` counter in state tracks loop depth

---

## Architecture decisions

| Decision | Choice | Reason |
|---|---|---|
| LLM output format | Structured JSON (Pydantic model) | Separates generation from rendering; enables deterministic docx rendering without an LLM |
| CV loading | PDF text extraction via `pypdf` | CV lives at `settings.cv_path`; cached in state after first read to avoid re-reading on revision |
| Company research | `httpx` web search, cached in `company_cache` table | Zero LLM tokens; results reused across runs for same company |
| Pre-write interview | Single multi-question interrupt | All gap questions surfaced at once; user answers in one pass before generation begins |
| Revision loop depth | Max 3 rounds (`settings.max_revision_rounds`) | After 3 rounds without approval: `docs_draft` status + warning, user re-enters manually |
| Document rendering | `python-docx` from structured JSON | No LLM in render path; page length is a post-render manual check in v1 |
| Hyperlinks in docx | XML manipulation (unpack/edit/repack) | `python-docx` doesn't natively support hyperlink runs; same constraint as the npm `docx` library |
| One-page enforcement | Estimated character count heuristic | LibreOffice headless length measurement deferred to v2 |

---

## New pure functions (unit testable)

Same pattern as Phase 2 — extract all parsing/generation logic into pure
functions that tests can call without a live LLM or file system.

```python
# writer.py
def _load_cv_text(cv_path: str) -> str
def _extract_job_gaps(cv_text: str, job_description: str) -> list[str]
def _build_resume_prompt(cv_text: str, job: JobListing, answers: dict) -> str
def _build_cover_letter_prompt(cv_text: str, job: JobListing, answers: dict) -> str
def _parse_resume_json(raw: str) -> ResumeContent          # Pydantic model
def _parse_cover_letter_json(raw: str) -> CoverLetterContent  # Pydantic model
def _render_resume_docx(content: ResumeContent, output_path: str) -> str
def _render_cover_letter_docx(content: CoverLetterContent, output_path: str) -> str
```

**New Pydantic models (add to `state.py`):**

```python
class ResumeSection(BaseModel):
    heading: str
    bullets: list[str]

class ResumeContent(BaseModel):
    name: str
    contact: str
    summary: str
    sections: list[ResumeSection]
    skills: list[str]

class CoverLetterContent(BaseModel):
    opening: str
    body_paragraphs: list[str]
    closing: str
```

---

## Test plan (TDD — write tests first, all red, then implement)

**New dependency to add to `pyproject.toml`:**
```toml
"pypdf>=4.0.0",
```

### Commit 1 — `build: add pypdf dependency`
No tests needed. Verify `uv sync` completes cleanly in Docker.

### Commit 2 — `test(phase3): add fixture data for writer tests`

New files:
- `tests/fixtures/sample_cv.txt` — minimal synthetic CV text (not a real PDF; loaded directly in tests)
- `tests/fixtures/sample_job.json` — one complete `JobListing` as JSON (loaded via `JobListing.model_validate_json()`)
- `tests/fixtures/sample_resume_llm_response.json` — realistic LLM JSON output for `ResumeContent`
- `tests/fixtures/sample_cover_letter_llm_response.json` — realistic LLM JSON output for `CoverLetterContent`

### Commit 3 — `test(phase3): write failing tests for pure writer functions`

**File:** `tests/test_phase3.py` — all red on first run.

**CV loading — 2 tests:**
- `test_load_cv_text_reads_file` → reads a text file, returns string content
- `test_load_cv_text_missing_file_raises` → missing path raises `FileNotFoundError`

**Gap extraction — 3 tests:**
- `test_extract_job_gaps_finds_missing_skills` → JD mentions "Kubernetes", CV doesn't → gap returned
- `test_extract_job_gaps_no_gaps` → all JD keywords present in CV → empty list
- `test_extract_job_gaps_case_insensitive` → "kubernetes" vs "Kubernetes" → not a false gap

**Prompt builders — 2 tests:**
- `test_build_resume_prompt_contains_job_title` → job title appears in prompt string
- `test_build_cover_letter_prompt_contains_company` → company name appears in prompt string

**JSON parsers — 4 tests:**
- `test_parse_resume_json_happy_path` → fixture JSON → valid `ResumeContent`
- `test_parse_resume_json_missing_field_raises` → malformed JSON → `ValidationError`
- `test_parse_cover_letter_json_happy_path` → fixture JSON → valid `CoverLetterContent`
- `test_parse_cover_letter_json_missing_field_raises` → malformed JSON → `ValidationError`

**docx rendering — 4 tests (no LLM, no file system — use `tmp_path`):**
- `test_render_resume_docx_creates_file` → `ResumeContent` fixture → `.docx` file created at `tmp_path`
- `test_render_resume_docx_contains_name` → rendered docx text contains candidate name
- `test_render_cover_letter_docx_creates_file` → `CoverLetterContent` fixture → `.docx` created
- `test_render_cover_letter_docx_contains_opening` → rendered docx text contains opening line

**Node behavior — 3 tests (monkeypatched Anthropic client):**
- `test_write_resume_calls_llm_once` → verify single API call made, returns resume path
- `test_apply_feedback_increments_revision_round` → `revision_round` goes from 0 → 1
- `test_should_revise_routes_correctly` → test all three branches of `should_revise`

**Total: 18 new tests, all red at commit time.**

### Commit 4 — `feat(state): add ResumeContent and CoverLetterContent models`

**File:** `src/pipeline/state.py`

Add `ResumeSection`, `ResumeContent`, `CoverLetterContent` Pydantic models.
No node implementation yet. Parser and prompt builder tests go green (partial).

### Commit 5 — `feat(writer): implement pure helper functions`

**File:** `src/pipeline/agents/writer.py`

Implement all eight pure functions listed above. No LLM calls yet.
CV loading, gap extraction, prompt builders, JSON parsers, docx renderers.
All pure function tests should go green. Node behavior tests still red.

**Key implementation notes:**
- `_load_cv_text`: use `pypdf.PdfReader` if path ends in `.pdf`, else `Path.read_text()`
- `_extract_job_gaps`: simple keyword extraction — tokenize JD, check against CV text; no LLM
- `_render_resume_docx`: use `python-docx`; hyperlinks require XML manipulation (see note below)
- One-page heuristic: warn if total character count exceeds 3,500 (rough proxy for one page)

**Hyperlink XML pattern for python-docx:**
```python
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
# Add relationship to document
r_id = doc.part.relate_to(url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink", is_external=True)
# Build hyperlink XML element manually and append to paragraph
```

### Commit 6 — `feat(writer): implement LLM nodes with Anthropic SDK`

**File:** `src/pipeline/agents/writer.py`

Implement `write_resume`, `write_cover_letter`, `apply_feedback`, `persist_documents`.

**LLM call pattern:**
```python
import anthropic
client = anthropic.Anthropic()
message = client.messages.create(
    model=LLM_MODEL,
    max_tokens=4096,
    system="You are a professional resume writer...",
    messages=[{"role": "user", "content": prompt}]
)
raw = message.content[0].text
content = _parse_resume_json(raw)
```

**Prompt caching:** Add `cache_control` to the CV + system prompt portions
(they don't change between revision rounds — significant cost saving).

**`apply_feedback` pattern:** Pass previous resume JSON + user feedback as a
targeted revision prompt, not a full regeneration. Cheaper and more accurate.

All 18 Phase 3 tests should pass after this commit.

### Commit 7 — `feat(writer): implement pre_write_interview and review_interrupt`

**File:** `src/pipeline/agents/writer.py`

Implement the two interrupt gates. Update `cli.py` to handle both interrupts
in the `write` command (same `interrupt_before` + `Command(resume)` pattern as discover).

**`pre_write_interview` interrupt value:**
```python
interrupt({
    "gaps": gaps,           # list[str] from _extract_job_gaps
    "questions": questions, # list of targeted questions based on gaps
    "message": "Answer these questions before document generation begins",
})
```

**`review_interrupt` interrupt value:**
```python
interrupt({
    "resume_path": state["resume_path"],
    "cover_letter_path": state["cover_letter_path"],
    "message": "Review documents. Reply: 'approve', 'abort', or provide feedback.",
})
```

**`should_revise` routing (already stubbed, just verify):**
- `human_feedback == "approve"` or `None` + `revision_round == 0` → `"persist_documents"`
- `human_feedback` is feedback text + `revision_round < max_rounds` → `"apply_feedback"`
- `revision_round >= max_rounds` → `"warn_and_exit"`

### Commit 8 — `feat(writer): implement load_job, fetch_cv, research_company`

**File:** `src/pipeline/agents/writer.py`

Implement the three setup nodes that run before the interview gate.

**`load_job`:** Query `jobs` table by `current_job_id`, populate state with `JobListing`.

**`fetch_cv`:** Call `_load_cv_text(settings.cv_path)`. Cache result in state so revision
rounds don't re-read the file. Add `cv_text: str | None` to `PipelineState`.

**`research_company`:** Check `company_cache` table first. If stale (> 7 days) or
missing, run `httpx` web search for company name + "headcount mission". Write to cache.
Add `company_context: str | None` to `PipelineState`.

**State additions needed (update `state.py`):**
```python
class PipelineState(TypedDict):
    # ... existing fields ...
    cv_text: str | None           # cached after fetch_cv
    company_context: str | None   # cached from research_company
    resume_content: ResumeContent | None    # structured LLM output
    cover_letter_content: CoverLetterContent | None
```

### Commit 9 — `docs: update README for Phase 3 completion`

- Phase table: Phase 3 ✅, Phase 4 🔜
- Update test count
- Add Writer section explaining the revision loop pattern

---

## Files created or modified in Phase 3

| File | Action |
|---|---|
| `pyproject.toml` | Add `pypdf>=4.0.0` |
| `src/pipeline/state.py` | Add `ResumeContent`, `CoverLetterContent`, `cv_text`, `company_context` fields |
| `src/pipeline/agents/writer.py` | Full implementation — all stubs replaced |
| `src/pipeline/cli.py` | Update `write` command with interrupt/resume handling |
| `tests/fixtures/sample_cv.txt` | New — synthetic CV text |
| `tests/fixtures/sample_job.json` | New — complete `JobListing` JSON |
| `tests/fixtures/sample_resume_llm_response.json` | New — realistic LLM output |
| `tests/fixtures/sample_cover_letter_llm_response.json` | New — realistic LLM output |
| `tests/test_phase3.py` | New — 18 tests |
| `README.md` | Updated |

**Expected final test count: 69/69** (51 existing + 18 new)

---

## Open questions to resolve at session start

1. **Resume format:** The master resume uses a table-based layout in the `.docx`.
   Should `_render_resume_docx` reproduce that exact layout, or use a simpler
   single-column structure for v1? (Simpler is faster to build; layout can be
   refined in Phase 6 polish.)

2. **`pre_write_interview` question generation:** Should gap questions be
   hardcoded templates (fast, predictable) or generated by a small LLM call
   (more context-aware, costs tokens)? Given the token minimization principle,
   hardcoded templates are the right call for v1.

3. **Output directory:** Generated `.docx` files go to `settings.output_dir`
   (`./output/` by default). Naming convention: `{job_id}_resume.docx` and
   `{job_id}_cover_letter.docx`. Confirm before implementing `persist_documents`.
