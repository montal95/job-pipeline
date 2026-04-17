"""
CLI entry point.

All subcommands are wired. Phase 1 implements the full discover flow
including interrupt-based triage.

Usage (after `uv pip install -e .`):
  pipeline discover
  pipeline discover --query "rails engineer" --location "Chicago, IL"
  pipeline write <job_id>
  pipeline submit <job_id>
  pipeline track
  pipeline run
  pipeline db migrate
  pipeline config set ANTHROPIC_API_KEY sk-ant-...
  pipeline config edit
  pipeline config show
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import warnings
import logging

# Force UTF-8 output on Windows — prevents UnicodeEncodeError on rich unicode
# symbols (✓, ✗, →, etc.) when the console code page is cp1252.
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.platform == "win32" and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# TODO: Remove these suppression blocks once pipeline.state types are properly registered
# in LangGraph's msgpack allow-list. Proper fix: register via `allowed_msgpack_modules`
# once the LangGraph API stabilizes. grep: "Deserializing unregistered type"
_original_showwarning = warnings.showwarning
def _filtered_showwarning(message, *args, **kwargs):
    msg = str(message)
    if any(x in msg for x in ("Deserializing unregistered type", "Core Pydantic V1", "tool.uv.dev-dependencies")):
        return
    _original_showwarning(message, *args, **kwargs)
warnings.showwarning = _filtered_showwarning
logging.getLogger("langgraph").setLevel(logging.ERROR)
logging.getLogger("langchain_core").setLevel(logging.ERROR)
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import typer
from rich.console import Console
from rich.table import Table
from rich import box

console = Console(legacy_windows=False)
app = typer.Typer(
    name="pipeline",
    help="LangGraph multi-agent job application pipeline.",
    add_completion=False,
)
db_app = typer.Typer(help="Database management commands.")
app.add_typer(db_app, name="db")

config_app = typer.Typer(help="Configure pipeline settings (.env).")
app.add_typer(config_app, name="config")


def run(coro):
    """Convenience wrapper to run an async function from a sync typer command."""
    return asyncio.run(coro)


# ── Config helpers (pure — unit testable) ────────────────────────────────────

# Sentinel used to distinguish "key not present" from "key present but empty"
_MISSING = object()

# Keys whose values are masked in `pipeline config show`
_SECRET_KEYS = {"ANTHROPIC_API_KEY"}


def _find_env_path() -> Path:
    """Return the .env path relative to the project root (two levels up from this file)."""
    return Path(__file__).parent.parent.parent / ".env"


def _read_env_keys(env_path: Path) -> dict[str, str]:
    """
    Parse a .env file into a dict. Skips blank lines and comments.
    Values may be optionally quoted — quotes are stripped.
    """
    if not env_path.exists():
        return {}
    result: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        result[key] = value
    return result


def _set_env_key(key: str, value: str, env_path: Path) -> None:
    """
    Write or update KEY=VALUE in a .env file.
    - If the key already exists on any line, that line is replaced in place.
    - If the key is absent, it is appended.
    - If the file doesn't exist, it is created.
    Preserves all other lines (comments, blank lines, other keys) exactly.
    """
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    lines = existing.splitlines(keepends=True)

    pattern = re.compile(rf"^{re.escape(key)}\s*=", re.IGNORECASE)
    replaced = False
    new_lines: list[str] = []

    for line in lines:
        if pattern.match(line):
            new_lines.append(f"{key}={value}\n")
            replaced = True
        else:
            new_lines.append(line)

    if not replaced:
        # Ensure file ends with a newline before appending
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines.append("\n")
        new_lines.append(f"{key}={value}\n")

    env_path.write_text("".join(new_lines), encoding="utf-8")


# ── Config commands ────────────────────────────────────────────────────────────


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Environment variable name (e.g. ANTHROPIC_API_KEY)"),
    value: str = typer.Argument(..., help="Value to set"),
    env_file: str = typer.Option("", "--env-file", help="Path to .env file (default: project root .env)"),
):
    """Set a key in the .env file. Creates the file if it doesn't exist."""
    env_path = Path(env_file) if env_file else _find_env_path()
    _set_env_key(key.upper(), value, env_path)
    display_value = "***" if key.upper() in _SECRET_KEYS else value
    console.print(f"[green]✓[/green] {key.upper()}={display_value} written to [dim]{env_path}[/dim]")


@config_app.command("edit")
def config_edit(
    env_file: str = typer.Option("", "--env-file", help="Path to .env file (default: project root .env)"),
):
    """Open the .env file in your system editor (notepad on Windows, $EDITOR/nano on Linux)."""
    env_path = Path(env_file) if env_file else _find_env_path()
    if not env_path.exists():
        console.print(f"[yellow].env not found at {env_path} — creating from .env.example[/yellow]")
        example = env_path.parent / ".env.example"
        if example.exists():
            env_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            env_path.touch()

    if sys.platform == "win32":
        editor = os.environ.get("EDITOR", "notepad")
    else:
        editor = os.environ.get("EDITOR", "nano")

    console.print(f"[dim]Opening {env_path} with {editor}...[/dim]")
    subprocess.run([editor, str(env_path)])


@config_app.command("show")
def config_show(
    env_file: str = typer.Option("", "--env-file", help="Path to .env file (default: project root .env)"),
):
    """Show current .env values. Secret keys are masked."""
    env_path = Path(env_file) if env_file else _find_env_path()
    keys = _read_env_keys(env_path)

    if not keys:
        console.print(f"[yellow]No .env found at {env_path} or file is empty.[/yellow]")
        console.print("Run [bold]pipeline config set ANTHROPIC_API_KEY <your-key>[/bold] to get started.")
        return

    table = Table(title=f".env — {env_path}", box=box.ROUNDED, show_lines=False)
    table.add_column("Key", style="cyan", no_wrap=True)
    table.add_column("Value", style="white")

    for k, v in sorted(keys.items()):
        display = "***" if k in _SECRET_KEYS else v
        table.add_row(k, display)

    console.print(table)


# ── Triage UI helpers ──────────────────────────────────────────────────────────


def _render_triage_table(listings: list[dict]) -> None:
    """Render the shortlist as a Rich table for user review."""
    table = Table(
        title=f"[bold]Shortlist — {len(listings)} listings[/bold]",
        box=box.ROUNDED,
        show_lines=True,
        expand=True,
    )
    table.add_column("#", style="dim", width=3, no_wrap=True)
    table.add_column("Title", style="bold white", min_width=24)
    table.add_column("Company", style="cyan", min_width=18)
    table.add_column("Location", style="green", min_width=14)
    table.add_column("Source", style="dim", width=8)
    table.add_column("Salary", style="yellow", width=18)
    table.add_column("Notes", style="dim italic", min_width=16)

    for i, job in enumerate(listings, start=1):
        comp_low = job.get("compensation_low")
        comp_high = job.get("compensation_high")
        if comp_low and comp_high and comp_low != comp_high:
            salary = f"${comp_low // 1000}K–${comp_high // 1000}K"
        elif comp_low:
            salary = f"${comp_low // 1000}K"
        else:
            salary = "—"
        notes = job.get("notes") or ""
        table.add_row(
            str(i),
            job.get("title", ""),
            job.get("company", ""),
            job.get("location", ""),
            job.get("source", ""),
            salary,
            notes,
        )
    console.print(table)


def _collect_triage_decisions(listings: list[dict]) -> dict:
    """
    Interactively prompt the user to mark each listing apply / skip / save.
    Returns {"apply": [id, ...], "skip": [id, ...]} for graph resume.
    """
    _render_triage_table(listings)
    console.print("\n[bold]Mark each listing:[/bold] [green]a[/green]=apply  [red]s[/red]=skip  [dim]Enter[/dim]=skip\n")

    apply_ids: list[str] = []
    skip_ids: list[str] = []

    for i, job in enumerate(listings, start=1):
        label = f"  [{i}] {job.get('title', '')} @ {job.get('company', '')}"
        choice = console.input(f"{label}  [a/s]: ").strip().lower()
        if choice == "a":
            apply_ids.append(job["id"])
            console.print(f"     [green]✓ Apply[/green]")
        else:
            skip_ids.append(job["id"])
            console.print(f"     [dim]— Skip[/dim]")

    console.print(f"\n[bold green]{len(apply_ids)} to apply[/bold green]  |  [dim]{len(skip_ids)} skipped[/dim]\n")
    return {"apply": apply_ids, "skip": skip_ids}


# ── Discovery ──────────────────────────────────────────────────────────────────


@app.command()
def discover(
    query: str = typer.Option("software engineer", "--query", "-q", help="Job title or keyword"),
    location: str = typer.Option("Chicago, IL", "--location", "-l", help="Location filter"),
    remote: bool = typer.Option(True, "--remote/--no-remote", help="Include remote roles"),
    sources: str = typer.Option("indeed,dice", "--sources", "-s", help="Comma-separated sources"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run/--no-dry-run",
        help="Preview results without writing to the database.",
    ),
):
    """Search configured job sources and present an interactive triage shortlist."""
    run(_discover(query, location, remote, sources, dry_run))


async def _discover(query: str, location: str, remote: bool, sources: str, dry_run: bool = False):
    from langgraph.errors import GraphInterrupt
    from langgraph.types import Command

    from pipeline.agents.discoverer import build_discoverer_graph
    from pipeline.state import SearchParams, empty_state
    from pipeline.state import SearchParams as _SearchParams

    thread_id = f"discover-{datetime.utcnow().strftime('%Y-%m-%dT%H-%M-%S')}"
    console.print(f"[bold green]Discoverer[/bold green] — thread: [dim]{thread_id}[/dim]")

    checkpointer_path = "./data/checkpoints.db"
    import aiosqlite
    from pathlib import Path
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    import langgraph.checkpoint.serde.jsonplus as _serde
    # Register pipeline state types to suppress deserialization warnings
    try:
        from langgraph.checkpoint.base import _allowed_msgpack_modules  # type: ignore
        _allowed_msgpack_modules.add("pipeline.state")
    except Exception:
        pass

    Path(checkpointer_path).parent.mkdir(parents=True, exist_ok=True)

    source_list = [s.strip() for s in sources.split(",") if s.strip()]
    initial = empty_state()
    initial["search_params"] = SearchParams(
        query=query, location=location, remote=remote, sources=source_list
    )
    initial["dry_run"] = dry_run
    config = {"configurable": {"thread_id": thread_id}}

    async with AsyncSqliteSaver.from_conn_string(checkpointer_path) as checkpointer:
        graph = build_discoverer_graph().compile(checkpointer=checkpointer, interrupt_before=["triage_interrupt"])

        # First invoke: runs scrapers → merge → pauses before triage_interrupt
        try:
            await graph.ainvoke(initial, config=config)
        except GraphInterrupt:
            pass  # Expected — graph suspended at interrupt boundary

        # Fetch state at the interrupt boundary to get the shortlist
        snap = await graph.aget_state(config)
        shortlist_raw = snap.values.get("shortlist", [])

        if not shortlist_raw:
            console.print("[yellow]No listings found to triage.[/yellow]")
            return

        # Collect user decisions interactively
        decisions = _collect_triage_decisions(
            [j.model_dump(mode="json") if hasattr(j, "model_dump") else j for j in shortlist_raw]
        )

        if not decisions["apply"]:
            console.print("[dim]No listings approved. Nothing saved.[/dim]")
            return

        # Resume graph past the interrupt with the user's decisions
        await graph.ainvoke(Command(resume=decisions), config=config)
        console.print("[bold green]✓ Discovery complete.[/bold green]")


# ── Write UI helpers ───────────────────────────────────────────────────────────


def _collect_interview_answers(interrupt_val: dict) -> dict:
    """
    Present gap questions to the user and collect answers.
    Returns a dict mapping question text → answer string.
    """
    questions: list[str] = interrupt_val.get("questions", [])
    gaps: list[str] = interrupt_val.get("gaps", [])

    if not questions:
        console.print("[dim]No gaps found — proceeding with document generation.[/dim]")
        return {}

    console.print(f"\n[bold yellow]Pre-write interview[/bold yellow] — {len(gaps)} gap(s) identified\n")
    answers: dict[str, str] = {}
    for i, question in enumerate(questions, start=1):
        console.print(f"  [cyan][{i}][/cyan] {question}")
        answer = console.input("  → ").strip()
        answers[question] = answer

    console.print()
    return answers


def _collect_review_decision(interrupt_val: dict) -> str:
    """
    Show resume preview and prompt for approve / abort / feedback.
    Cover letter is shown separately at the cl_review_interrupt gate.
    Returns "approve", "abort", or a feedback string.
    """
    resume_preview = interrupt_val.get("resume_preview", "(no resume content)")
    resume_content = interrupt_val.get("resume_content")

    console.print("\n[bold cyan]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/bold cyan]")
    console.print("[bold]RESUME PREVIEW[/bold]")
    console.print("[bold cyan]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/bold cyan]\n")

    if resume_content and hasattr(resume_content, "name"):
        console.print(f"[bold]{resume_content.name}[/bold]")
        # Contact: two lines split on \n — line 1: city/phone/email, line 2: socials
        contact_lines = resume_content.contact.split("\\n") if "\\n" in resume_content.contact else resume_content.contact.split("\n")
        for line in contact_lines:
            console.print(f"[dim]{line.strip()}[/dim]")
        console.print()
        console.print(f"[bold]Summary[/bold]\n{resume_content.summary}\n")

        # Skills — escape Rich markup brackets, strip any leftover ** markers
        if resume_content.skills:
            console.print(f"[bold yellow]Skills[/bold yellow]")
            for skill in resume_content.skills:
                clean = skill.replace("**", "")
                console.print(f"  {clean}", markup=False, highlight=False)
            console.print()

        for section in resume_content.sections:
            # Skip empty sections (e.g. LLM put "SKILLS & TECHNOLOGIES" as a section with no bullets)
            if not section.bullets:
                continue
            console.print(f"[bold yellow]{section.heading}[/bold yellow]")
            for bullet in section.bullets:
                clean = bullet.replace("**", "").strip()
                # Employer line: "Company · Location | Role | Dates"
                if "|" in clean and "·" in clean:
                    console.print(f"\n  [bold]{clean}[/bold]")
                # Project line: "Project Name — description"
                elif " — " in clean:
                    console.print(f"\n    [bold cyan]{clean}[/bold cyan]")
                # Stack line
                elif clean.startswith("Stack:"):
                    console.print(f"    {clean}", markup=False, highlight=False)
                else:
                    console.print(f"    • {clean}", markup=False, highlight=False)
            console.print()
    else:
        console.print(resume_preview)

    console.print("\n[bold cyan]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/bold cyan]\n")
    console.print("  [green]y / yes[/green] — accept and save")
    console.print("  [red]n / no[/red]   — discard and exit")
    console.print("  [dim]<feedback>[/dim] — type feedback to revise\n")

    decision = console.input("  Decision: ").strip().lower()
    if decision in ("y", "yes"):
        return "approve"
    if decision in ("n", "no"):
        return "abort"
    return decision if decision else "approve"


def _collect_cl_decision(interrupt_val: dict) -> str:
    """
    Show cover letter preview and prompt for approve / abort / feedback.
    Returns "approve", "abort", or a feedback string.
    """
    cl_content = interrupt_val.get("cover_letter_content")

    console.print("\n[bold cyan]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/bold cyan]")
    console.print("[bold]COVER LETTER PREVIEW[/bold]")
    console.print("[bold cyan]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/bold cyan]\n")

    if cl_content and hasattr(cl_content, "opening"):
        console.print(cl_content.opening, markup=False, highlight=False)
        console.print()
        for para in cl_content.body_paragraphs:
            console.print(para, markup=False, highlight=False)
            console.print()
        console.print(cl_content.closing, markup=False, highlight=False)
    else:
        console.print(interrupt_val.get("cover_letter_preview", "(no cover letter content)"))

    console.print("\n[bold cyan]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/bold cyan]\n")
    console.print("  [green]y / yes[/green] — accept and save")
    console.print("  [red]n / no[/red]   — discard and exit")
    console.print("  [dim]<feedback>[/dim] — type feedback to revise\n")

    decision = console.input("  Decision: ").strip().lower()
    if decision in ("y", "yes"):
        return "approve"
    if decision in ("n", "no"):
        return "abort"
    return decision if decision else "approve"


@app.command()
def write(
    job_id: str = typer.Argument(..., help="Job ID from the database"),
    resume_only: bool = typer.Option(False, "--resume-only", "-r", help="Generate and review resume only, skip cover letter."),
    cover_letter_only: bool = typer.Option(False, "--cover-letter-only", "-c", help="Generate and review cover letter only, skip resume."),
):
    """Generate tailored resume and cover letter for a specific job."""
    run(_write(job_id, resume_only=resume_only, cover_letter_only=cover_letter_only))


async def _write(job_id: str, resume_only: bool = False, cover_letter_only: bool = False):
    from pathlib import Path

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.errors import GraphInterrupt
    from langgraph.types import Command

    from pipeline.agents.writer import build_writer_graph
    from pipeline.state import empty_state

    thread_id = f"write-{job_id}"
    checkpointer_path = "./data/checkpoints.db"
    Path(checkpointer_path).parent.mkdir(parents=True, exist_ok=True)

    console.print(f"[bold blue]Writer[/bold blue] — job: {job_id} | thread: [dim]{thread_id}[/dim]")

    config = {"configurable": {"thread_id": thread_id}}

    async with AsyncSqliteSaver.from_conn_string(checkpointer_path) as checkpointer:
        graph = build_writer_graph().compile(checkpointer=checkpointer)

        initial = empty_state()
        initial["current_job_id"] = job_id
        initial["write_resume_only"] = resume_only
        initial["write_cover_letter_only"] = cover_letter_only

        # First invoke — runs load_job → fetch_cv → research_company
        # then hits pre_write_interview interrupt
        try:
            await graph.ainvoke(initial, config=config)
        except GraphInterrupt:
            pass

        # Drive the graph through all interrupt gates
        while True:
            snap = await graph.aget_state(config)

            if not snap.next:
                console.print("[bold green]✓ Documents saved.[/bold green]")
                break

            # Extract the interrupt value from the paused task
            interrupt_data: dict = {}
            if snap.tasks and snap.tasks[0].interrupts:
                interrupt_data = snap.tasks[0].interrupts[0].value or {}

            if "questions" in interrupt_data:
                # pre_write_interview gate
                answers = _collect_interview_answers(interrupt_data)
                try:
                    await graph.ainvoke(Command(resume=answers), config=config)
                except GraphInterrupt:
                    pass

            elif interrupt_data.get("gate") == "resume":
                # resume_review_interrupt — shows resume + CL (combined in full flow)
                decision = _collect_review_decision(interrupt_data)
                if decision.strip().lower() in ("abort", "n", "no"):
                    console.print("[yellow]Application aborted.[/yellow]")
                    break
                try:
                    await graph.ainvoke(Command(resume=decision), config=config)
                except GraphInterrupt:
                    pass

            elif interrupt_data.get("gate") == "cover_letter":
                # cl_review_interrupt — dedicated CL revision loop
                decision = _collect_cl_decision(interrupt_data)
                if decision.strip().lower() in ("abort", "n", "no"):
                    console.print("[yellow]Application aborted.[/yellow]")
                    break
                try:
                    await graph.ainvoke(Command(resume=decision), config=config)
                except GraphInterrupt:
                    pass

            else:
                # Unknown interrupt or graph in unexpected state — exit safely
                console.print(f"[dim]Graph paused at: {snap.next} — exiting.[/dim]")
                break


# ── Submit ─────────────────────────────────────────────────────────────────────


@app.command()
def submit(
    job_id: str = typer.Argument(..., help="Job ID from the database"),
):
    """Fill and submit the ATS form for a specific job."""
    run(_submit(job_id))


async def _submit(job_id: str):
    from pathlib import Path

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.errors import GraphInterrupt
    from langgraph.types import Command

    from pipeline.agents.submitter import build_submitter_graph
    from pipeline.state import empty_state

    thread_id = f"submit-{job_id}"
    checkpointer_path = "./data/checkpoints.db"
    Path(checkpointer_path).parent.mkdir(parents=True, exist_ok=True)

    console.print(f"[bold yellow]Submitter[/bold yellow] — job: {job_id} | thread: [dim]{thread_id}[/dim]")

    config = {"configurable": {"thread_id": thread_id}}

    async with AsyncSqliteSaver.from_conn_string(checkpointer_path) as checkpointer:
        graph = build_submitter_graph().compile(checkpointer=checkpointer)

        initial = empty_state()
        initial["current_job_id"] = job_id

        try:
            await graph.ainvoke(initial, config=config)
        except GraphInterrupt:
            pass

        while True:
            snap = await graph.aget_state(config)

            if not snap.next:
                # Check final state for warnings/errors
                final = snap.values
                for w in final.get("warnings", []):
                    console.print(f"[yellow]⚠ {w}[/yellow]")
                if final.get("errors"):
                    for e in final.get("errors", []):
                        console.print(f"[red]✗ {e}[/red]")
                elif final.get("submission_confirmed"):
                    console.print("[bold green]✓ Application submitted and confirmed.[/bold green]")
                else:
                    console.print("[dim]Submitter run complete.[/dim]")
                break

            interrupt_data: dict = {}
            if snap.tasks and snap.tasks[0].interrupts:
                interrupt_data = snap.tasks[0].interrupts[0].value or {}

            if "form_summary" in interrupt_data:
                # submission_gate — show summary, collect yes/no
                summary = interrupt_data["form_summary"]
                table = Table(title="Form Summary", box=box.SIMPLE)
                table.add_column("Field", style="dim")
                table.add_column("Value")
                table.add_row("Company", summary.get("company", ""))
                table.add_row("Role", summary.get("title", ""))
                table.add_row("ATS", str(summary.get("ats_type", "")))
                table.add_row("Fields filled", ", ".join(summary.get("fields_filled", [])) or "none")
                table.add_row("File attached", "yes" if summary.get("file_attached") else "no")
                console.print(table)

                decision = typer.prompt(
                    "Submit this application? (yes / abort)",
                    default="abort",
                ).strip().lower()
                try:
                    await graph.ainvoke(Command(resume=decision), config=config)
                except GraphInterrupt:
                    pass

            else:
                console.print(f"[dim]Graph paused at: {snap.next} — exiting.[/dim]")
                break


# ── Track ──────────────────────────────────────────────────────────────────────


@app.command()
def track():
    """Show pipeline status dashboard and manage follow-ups."""
    run(_track())


async def _track():
    from pathlib import Path

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from pipeline.agents.tracker import build_tracker_graph
    from pipeline.state import empty_state

    thread_id = f"track-{datetime.utcnow().date()}"
    checkpointer_path = "./data/checkpoints.db"
    Path(checkpointer_path).parent.mkdir(parents=True, exist_ok=True)

    console.print(f"[bold magenta]Tracker[/bold magenta] — thread: [dim]{thread_id}[/dim]")

    config = {"configurable": {"thread_id": thread_id}}

    async with AsyncSqliteSaver.from_conn_string(checkpointer_path) as checkpointer:
        graph = build_tracker_graph().compile(checkpointer=checkpointer)
        await graph.ainvoke(empty_state(), config=config)

    console.print("[bold green]✓ Tracker complete.[/bold green]")


# ── Run (full pipeline) ────────────────────────────────────────────────────────


@app.command()
def run_pipeline(
    query: str = typer.Option("software engineer", "--query", "-q"),
    location: str = typer.Option("Chicago, IL", "--location", "-l"),
    remote: bool = typer.Option(True, "--remote/--no-remote"),
):
    """Full pipeline: discover → triage → write → submit for approved jobs."""
    run(_run_pipeline(query, location, remote))


async def _run_pipeline(query: str, location: str, remote: bool):
    """
    Full pipeline: discover → write queued jobs → submit → track.

    1. Run discovery with the given search params.
    2. After triage, query the DB for all jobs at status=queued.
    3. For each queued job, run writer then submitter.
    4. Run tracker dashboard at the end.
    """
    import sqlite3

    from pipeline.config import settings

    # Step 1: Discovery + triage
    await _discover(query, location, remote, settings.enabled_sources)

    # Step 2: Find jobs approved during triage (status=queued)
    db_path = str(settings.app_db_path)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE status = 'queued'"
        ).fetchall()
    finally:
        conn.close()

    queued_ids = [row[0] for row in rows]
    if not queued_ids:
        console.print("[dim]No queued jobs — skipping write/submit.[/dim]")
    else:
        console.print(f"[bold]{len(queued_ids)} job(s) queued — running writer + submitter...[/bold]")
        for job_id in queued_ids:
            await _write(job_id)
            await _submit(job_id)

    # Step 3: Tracker dashboard
    await _track()


# ── DB management ──────────────────────────────────────────────────────────────


@db_app.command("migrate")
def db_migrate():
    """Apply pending database migrations."""
    from pipeline.database import run_migrations
    console.print("[bold]Running migrations...[/bold]")
    asyncio.run(run_migrations())
    console.print("[green]✓ Migrations complete[/green]")


if __name__ == "__main__":
    app()
