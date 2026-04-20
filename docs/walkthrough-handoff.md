# job-pipeline — Walkthrough Handoff

**Branch:** checkpoint/discoverer-e2e-playwright-salary
**Tests:** 152 passing | **Last commit:** def716c

---

## Current state — Steps 1-7 complete

| Step | Status |
|------|--------|
| 1 Install | done |
| 2 Configure | done |
| 3 DB migrate | done |
| 4 Playwright auth (LinkedIn) | done |
| 5 Discovery — LinkedIn, Dice, ZipRecruiter, Built In | done |
| 6 Check queued | done — 25 jobs queued |
| 7 Write | done — full loop working with Gemini |
| 8 Submit | NEXT SESSION |
| 9 Full run | after submit |
| 10 Track | done — 1 docs_ready |

---

## Writer recap

Two sequential revision loops. Resume first, then cover letter.
Each loop: generate, preview, y/n/feedback, revise up to MAX_REVISION_ROUNDS.

CLI flags:
  pipeline write JOB_ID                  full flow (resume + CL)
  pipeline write JOB_ID --resume-only    skip CL
  pipeline write JOB_ID --cover-letter-only  skip resume

LLM: Gemini 2.5 Flash via google-genai SDK.
.env keys: LLM_PROVIDER=gemini, GEMINI_API_KEY, GEMINI_MODEL=gemini-2.5-flash
Switch to Anthropic: set LLM_PROVIDER=anthropic and ANTHROPIC_API_KEY.

---

## DB state

DB: data/test_pipeline.db | Checkpoints: data/checkpoints.db
25 jobs status=queued | 1 job status=docs_ready (f77c2194 - Docker SE role)

Query jobs:
  .\.venv\Scripts\python.exe -c "import sqlite3; conn=sqlite3.connect('data/test_pipeline.db'); conn.row_factory=sqlite3.Row; [print(r['status'],'|',r['id'][:8],'|',r['title'][:35],'@',r['company']) for r in conn.execute('SELECT id,title,company,status FROM jobs ORDER BY status,discovered_at DESC').fetchall()]; conn.close()"

Clear stale write checkpoint:
  .\.venv\Scripts\python.exe -c "import sqlite3; conn=sqlite3.connect('data/checkpoints.db'); conn.execute('DELETE FROM checkpoints WHERE thread_id LIKE ?',('write-JOBID_PREFIX%',)); conn.execute('DELETE FROM writes WHERE thread_id LIKE ?',('write-JOBID_PREFIX%',)); conn.commit(); conn.close(); print('Done')"

---

## Step 8 — Submit (next to build/test)

  & .\.venv\Scripts\pipeline.exe submit f77c2194-ad3f-4193-b65d-041332327444

Expected flow:
1. Loads job + doc JSON from DB
2. Detects ATS type from apply URL (Greenhouse/Workday/Ashby/unknown)
3. If ATS has file upload: renders .docx via _render_resume_docx / _render_cover_letter_docx
4. Fills form fields (name, email, phone, LinkedIn URL)
5. Pauses at submission gate — shows form summary, waits for yes
6. Submits, screenshots confirmation to ./output/
7. Updates job to status=applied, writes to submissions table

Check apply URL and ATS type first:
  .\.venv\Scripts\python.exe -c "import sqlite3; conn=sqlite3.connect('data/test_pipeline.db'); conn.row_factory=sqlite3.Row; r=conn.execute('SELECT apply_url,ats_type FROM jobs WHERE id=?',('f77c2194-ad3f-4193-b65d-041332327444',)).fetchone(); print('apply_url:',r['apply_url']); print('ats_type:',r['ats_type']); conn.close()"

ATS behavior:
  Greenhouse: full automation end-to-end
  Workday: manual URL fallback (bot detection) — known v1 limitation
  Unknown: fills what it can, asks for confirmation

---

## Rendering — deferred to submitter

render_resume_docx / render_cover_letter_docx are stub nodes in the writer graph.
_render_resume_docx / _render_cover_letter_docx in writer.py are implemented and tested.
Submitter calls them directly when ATS form has file upload — deferred render per PRD.

---

## Remaining walkthrough success criteria

[ ] pipeline submit reaches the submission gate and shows form summary
[ ] At least 1 application submits (Greenhouse path preferred)
[ ] Confirmation screenshot saved to ./output/
[ ] pipeline track shows 1+ status=applied
[ ] pipeline run chains discover to write to submit to track E2E

---

## Known issues

Gemini $0 cap blocks requests — raise to $5 in GCP console (cost <$0.01/application)
Workday bot detection — manual URL fallback, known v1 limitation
ZipRecruiter submitter auth — run save_auth.py --platform ziprecruiter before submitting ZR jobs
sqlite3 not on PATH — use .venv python -c "import sqlite3..." pattern
Stale write checkpoints — clear with python snippet above before re-running write on same job
