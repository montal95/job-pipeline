# Blog Post Outline — Building a LangGraph Job Application Pipeline

**Audience:** Software engineers interested in LangGraph, agentic AI systems, or
job search automation. Assumes Python fluency; no prior LangGraph experience required.

**Format:** Either one long post (~4,000 words) or a 4-part series.
**Raw material:** Commit history (`git log --oneline`), handoff docs in `docs/`,
session notes written during each build session.

---

## Series structure (recommended)

### Part 1 — Why I automated my job search and what I built

**Hook:** I applied to 40+ jobs manually. Then I built a pipeline to do it for me —
and learned more about agentic AI in the process than any tutorial taught me.

**Sections:**
- The problem: job hunting is repetitive, time-consuming, and hard to track
- Why LangGraph instead of a simple script
- Architecture overview: four agents, one SQLite database, CLI-first
- What "token minimization" means in practice: 2-3 LLM calls per application, everything
  else is scraping or deterministic Python
- Link to the repo; invite readers to follow along

**Key insight to land:** Most AI pipeline tutorials skip the boring parts — form parsing,
DB persistence, interrupt handling. This series doesn't.

---

### Part 2 — The Discoverer: parallel scraping with LangGraph's Send API

**Hook:** LangGraph's `Send` API lets you fan out to N parallel nodes and accumulate
results with a reducer — no threading, no asyncio.gather, just graph topology.

**Sections:**
- How the `Send` API works: fan_out_sources emits one Send per source, each runs
  concurrently, `operator.add` accumulates raw_results automatically
- Fingerprint dedup: sha256(company|title|location)[:16] — simple, deterministic,
  cross-source
- The triage interrupt gate: `interrupt()` checkpoints state so you can close the
  terminal and resume the next day
- Playwright auth sessions: why `storage_state` is the right pattern for scrapers
  that need login state
- BS4 gotcha: `soup.find_all("label", for_=True)` silently returns nothing because
  `for` is a reserved Python keyword. Use `attrs={"for": True}` instead.

**Code snippet:** The `fan_out_sources` function (10 lines); the interrupt/resume
pattern in the CLI loop.

---

### Part 3 — The Writer: cycles, revision loops, and structured LLM output

**Hook:** Getting an LLM to produce a consistent, renderable document requires treating
its output as structured data, not a final artifact.

**Sections:**
- Why `ResumeContent` / `CoverLetterContent` Pydantic models, not free-form text
  — the LLM returns JSON, Pydantic validates it, python-docx renders it deterministically
- The revision cycle: `apply_feedback → write_resume` as a LangGraph back-edge.
  How to model "loop until approved" without infinite recursion (max rounds + warn_and_exit)
- Two interrupt gates: `pre_write_interview` extracts JD gaps and asks targeted questions;
  `review_interrupt` shows a content preview and waits for approve/feedback/abort
- Hyperlinks in python-docx: the `docx` npm library and `python-docx` share the same
  limitation — neither supports hyperlink runs natively. OOXML manipulation is the fix.
- Deferred rendering (the key architectural decision): Writer persists JSON to DB,
  Submitter renders .docx only if the ATS form needs a file upload. Why this matters.

**Code snippet:** `_parse_resume_json` (handles markdown fence stripping);
`should_revise` routing function.

---

### Part 4 — The Submitter and Tracker: form automation and the hard interrupt

**Hook:** The submission gate is the most safety-critical pattern in the pipeline.
No form should ever be submitted without explicit human confirmation — and LangGraph's
`interrupt()` is the right tool for it.

**Sections:**

**Submitter:**
- Conditional render: scan the form HTML before rendering docs — skip the render
  entirely if the ATS doesn't need a file upload
- `_map_form_fields` and ATS strategies: Greenhouse uses `label[for]` → `#id`,
  Workday uses `aria-label` → CSS selector. Why keeping them separate makes the
  code extensible.
- The hard gate: `submission_gate` interrupts the graph, surfaces a form summary
  (company, role, ATS, fields filled, file attached), and requires `'yes'` to proceed.
  Nothing touches the submit button without this.
- The browser session problem: `interrupt()` checkpoints state and exits the process.
  The Playwright browser is gone when `submit_form` runs. Re-filling before submitting
  is not a workaround — it's required.

**Tracker:**
- LangGraph without LLMs: the Tracker proves the pattern is useful even for
  purely synchronous DB operations. No AI required to benefit from graph-based
  workflow orchestration.
- Pure functions as the testable core: `_count_by_status`, `_find_overdue`,
  `_format_days_since` — all unit-tested without DB or terminal dependencies.
- Node order matters: `schedule_followup` before `flag_overdue` before
  `render_dashboard`. Why the sequence is part of the specification.

**Closing:**
- What I'd do differently (v2 ideas: Docker containerization of Playwright,
  LangGraph Studio for visual debugging, notification integration)
- The build-in-public angle: this repo is both a working tool and a portfolio signal
- What "AI orchestration engineer" means in practice — it's mostly boring Python

---

## Standalone post option

If writing as one post, compress Parts 2-4 into:
- Section: "The four patterns that actually matter" (Send API, interrupt gates,
  revision cycles, conditional edges)
- Section: "What I learned about LangGraph that tutorials don't tell you"
  (BS4 gotcha, browser session problem, deferred rendering decision)
- Section: "The token minimization principle" (table from README)

---

## Writing notes

- **Lead with the problem, not the technology.** Readers care about job hunting
  before they care about LangGraph.
- **Use commit messages as section anchors.** The git log is the outline;
  each commit message is a section heading waiting to happen.
- **The gotchas are the most shareable content.** BS4's `for_` keyword problem,
  the browser session interrupt problem, `python-docx` hyperlinks — these are
  the things that will get bookmarked and shared.
- **Don't oversell.** The pipeline works but has real limitations (Workday manual,
  LinkedIn scraping ToS, no cloud deployment). Being honest about the rough edges
  builds credibility.
- **Cross-link to the repo** at every natural stopping point. The README is the
  companion document to the blog post series.

---

## Publication targets

- Primary: `sammontalvojr.com` (portfolio site) — drives SEO for "LangGraph tutorial",
  "agentic AI pipeline", "job application automation"
- Secondary: `sammontalvojr.medium.com` — cross-post for distribution
- LinkedIn article: condensed version (~800 words) linking to the full series
- Each post should reference the portfolio site and GitHub profile explicitly
