# job-pipeline v1.0.0 — Local Test Walkthrough

**Goal:** Install, configure, and run the pipeline end-to-end. Apply to 10 jobs across Indeed, Dice, LinkedIn, and ZipRecruiter.

**Repo:** `C:\Users\sammo\Code\job-pipeline` (already cloned, tagged v1.0.0)
**Run all commands in PowerShell from the repo root.**

---

## Before you start — gather these

| Item | Where to get it |
|------|----------------|
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys |
| Absolute path to your CV PDF | e.g. `C:\Users\sammo\Documents\Sam_Montalvo_CV.pdf` |
| 15 min for one-time Playwright auth | Just time |

---

## Step 1 — Install (run once)

```powershell
cd C:\Users\sammo\Code\job-pipeline

# Install uv if not present
winget install astral-sh.uv

# Install project dependencies
uv sync

# Verify CLI is wired
& .\.venv\Scripts\pipeline.exe --help
```

**Expected:** Help text listing `discover`, `write`, `submit`, `track`, `run`, `db`, `config`.

---

## Step 2 — Configure

```powershell
& .\.venv\Scripts\pipeline.exe config set ANTHROPIC_API_KEY sk-ant-YOUR-KEY-HERE
& .\.venv\Scripts\pipeline.exe config set CV_PATH "C:\Users\sammo\Documents\Sam_Montalvo_CV.pdf"
& .\.venv\Scripts\pipeline.exe config show
```

**Expected:** Table showing all keys. API key displays as `***`.

---

## Step 3 — Initialize the database

```powershell
& .\.venv\Scripts\pipeline.exe db migrate
```

**Expected:** Three migration lines applied, then `✓ Migrations complete`.

---

## Step 4 — Playwright auth (one-time, ~10 min)

This gives the pipeline your LinkedIn and ZipRecruiter sessions. Auth files last ~30 days.

```powershell
python scripts\save_auth.py --platform all
```

A Chrome window opens. Log in to LinkedIn in tab 1, ZipRecruiter in tab 2. Press Enter in the terminal when done.

**Want to skip for now?** Use `--sources "indeed,dice"` in Step 5. No auth needed. You'll get fewer listings but can test the full pipeline immediately.

---

## Step 5 — Discovery (target: approve 10+ listings)

```powershell
& .\.venv\Scripts\pipeline.exe discover `
  --query "software engineer rails" `
  --location "Chicago, IL" `
  --sources "indeed,dice,linkedin,ziprecruiter"
```

**What happens:**
1. All four scrapers run in parallel
2. Results deduplicated by fingerprint across sources
3. A Rich table displays every listing with title, company, location, salary

**At the triage prompt:** press `a` to approve, `s` or Enter to skip. Approve at least 10 — you review the actual resume before anything submits. Cast wide, be generous with approvals.

**After triage:** approved jobs saved to DB at `status=queued`.

---

## Step 6 — Check what was queued

```powershell
sqlite3 .\data\pipeline.db "SELECT id, title, company, status FROM jobs WHERE status='queued'"
```

Copy the IDs you want to work through. Or use `pipeline run` (Step 9) to chain everything automatically.

---

## Step 7 — Write documents (per job)

```powershell
& .\.venv\Scripts\pipeline.exe write JOB_ID_HERE
```

**Flow:**
1. Gap analysis — targeted questions about skills in the JD missing from your CV. Answer or press Enter to skip each.
2. LLM generates resume + cover letter (2 API calls)
3. Terminal shows a content preview
4. Type `approve` to save, `abort` to discard, or type feedback to revise

Repeat for each queued job. The revision loop allows up to 3 rounds before it saves as draft.

---

## Step 8 — Submit (per job, Greenhouse ATS)

```powershell
& .\.venv\Scripts\pipeline.exe submit JOB_ID_HERE
```

**Flow:**
1. Scans the apply URL for form structure
2. Renders .docx files only if form has file upload inputs
3. Fills form fields automatically (name, email, phone, LinkedIn URL)
4. **Pauses — shows a form summary. You must type `yes` to submit.** Anything else aborts safely.
5. Takes a confirmation screenshot to `.\output\`

**ATS-specific behavior:**
- **Greenhouse:** Full automation. Works end-to-end.
- **Workday:** Surfaces a warning with the apply URL. Complete manually in Chrome. Known v1 limitation.
- **Ashby / Unknown:** Fills what it can, then asks for confirmation. Check the form visually before typing `yes`.

---

## Step 9 — Full pipeline shortcut (optional)

Instead of Steps 5–8 individually:

```powershell
& .\.venv\Scripts\pipeline.exe run `
  --query "software engineer rails" `
  --location "Chicago, IL"
```

Chains: discover → triage → write all queued → submit all → track. Slower but hands-off between gates.

---

## Step 10 — Dashboard

```powershell
& .\.venv\Scripts\pipeline.exe track
```

Shows:
- Status counts across all jobs (new / queued / applied / rejected / offer)
- Active applications with applied date and follow-up due date
- ⚠ overdue indicator for anything past the 7-day follow-up window

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Indeed returns 0 or 403 | Normal — Indeed rate-limits scrapers. Try again or skip Indeed. |
| LinkedIn/ZipRecruiter shows stale session warning | Re-run `python scripts\save_auth.py --platform linkedin` |
| `write` hangs at "research_company" | DuckDuckGo API timeout — it recovers and continues with minimal context |
| Workday job hits manual fallback | Open the apply URL in Chrome yourself |
| LLM resume needs adjusting | Type feedback at the review gate; the revision loop tightens it |
| `uv: command not found` | Run the winget install line from Step 1 again, then open a new PowerShell |

---

## Useful DB queries

```powershell
# All jobs and their status
sqlite3 .\data\pipeline.db "SELECT id, title, company, status FROM jobs ORDER BY discovered_at DESC"

# Only queued (ready to write)
sqlite3 .\data\pipeline.db "SELECT id, title, company FROM jobs WHERE status='queued'"

# Applied — check follow-ups
sqlite3 .\data\pipeline.db "SELECT j.title, j.company, s.followup_due_date FROM jobs j JOIN submissions s ON s.job_id = j.id WHERE j.status='applied'"
```

---

## Success criteria

- [ ] `pipeline db migrate` runs clean
- [ ] `pipeline config show` shows API key and CV path
- [ ] Discovery returns results from at least 2 sources
- [ ] Triage approves 10+ listings saved at `status=queued`
- [ ] `pipeline write` produces a resume preview that looks correct
- [ ] At least 1 application reaches the submission gate
- [ ] `pipeline track` shows the dashboard with correct counts
