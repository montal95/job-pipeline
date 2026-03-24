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
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from uuid import uuid4

import typer
from rich.console import Console
from rich.table import Table
from rich import box

console = Console()
app = typer.Typer(
    name="pipeline",
    help="LangGraph multi-agent job application pipeline.",
    add_completion=False,
)
db_app = typer.Typer(help="Database management commands.")
app.add_typer(db_app, name="db")


def run(coro):
    """Convenience wrapper to run an async function from a sync typer command."""
    return asyncio.run(coro)


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
):
    """Search configured job sources and present an interactive triage shortlist."""
    run(_discover(query, location, remote, sources))


async def _discover(query: str, location: str, remote: bool, sources: str):
    from langgraph.errors import GraphInterrupt
    from langgraph.types import Command

    from pipeline.agents.discoverer import build_discoverer_graph
    from pipeline.state import SearchParams, empty_state

    thread_id = f"discover-{datetime.utcnow().date()}"
    console.print(f"[bold green]Discoverer[/bold green] — thread: [dim]{thread_id}[/dim]")

    checkpointer_path = "./data/checkpoints.db"
    import aiosqlite
    from pathlib import Path
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    Path(checkpointer_path).parent.mkdir(parents=True, exist_ok=True)

    source_list = [s.strip() for s in sources.split(",") if s.strip()]
    initial = empty_state()
    initial["search_params"] = SearchParams(
        query=query, location=location, remote=remote, sources=source_list
    )
    config = {"configurable": {"thread_id": thread_id}}

    async with await AsyncSqliteSaver.from_conn_string(checkpointer_path) as checkpointer:
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


# ── Write ──────────────────────────────────────────────────────────────────────


@app.command()
def write(
    job_id: str = typer.Argument(..., help="Job ID from the database"),
):
    """Generate tailored resume and cover letter for a specific job."""
    run(_write(job_id))


async def _write(job_id: str):
    from pipeline.graph import build_pipeline
    from pipeline.state import empty_state
    thread_id = f"write-{job_id}"
    console.print(f"[bold blue]Writer[/bold blue] — job: {job_id} | thread: {thread_id}")
    graph = await build_pipeline(thread_id)
    initial = empty_state()
    initial["current_job_id"] = job_id
    config = {"configurable": {"thread_id": thread_id}}
    await graph.ainvoke(initial, config=config)
    console.print("[dim]Writer run complete (Phase 3 target)[/dim]")


# ── Submit ─────────────────────────────────────────────────────────────────────


@app.command()
def submit(
    job_id: str = typer.Argument(..., help="Job ID from the database"),
):
    """Fill and submit the ATS form for a specific job."""
    run(_submit(job_id))


async def _submit(job_id: str):
    from pipeline.graph import build_pipeline
    from pipeline.state import empty_state
    thread_id = f"submit-{job_id}"
    console.print(f"[bold yellow]Submitter[/bold yellow] — job: {job_id} | thread: {thread_id}")
    graph = await build_pipeline(thread_id)
    initial = empty_state()
    initial["current_job_id"] = job_id
    config = {"configurable": {"thread_id": thread_id}}
    await graph.ainvoke(initial, config=config)
    console.print("[dim]Submitter run complete (Phase 4 target)[/dim]")


# ── Track ──────────────────────────────────────────────────────────────────────


@app.command()
def track():
    """Show pipeline status dashboard and manage follow-ups."""
    run(_track())


async def _track():
    from pipeline.graph import build_pipeline
    from pipeline.state import empty_state
    thread_id = f"track-{datetime.utcnow().date()}"
    console.print(f"[bold magenta]Tracker[/bold magenta] — thread: {thread_id}")
    graph = await build_pipeline(thread_id)
    config = {"configurable": {"thread_id": thread_id}}
    await graph.ainvoke(empty_state(), config=config)
    console.print("[dim]Tracker run complete (Phase 5 target)[/dim]")


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
    from pipeline.graph import build_pipeline
    from pipeline.state import SearchParams, empty_state
    thread_id = f"run-{datetime.utcnow().date()}-{str(uuid4())[:8]}"
    console.print(f"[bold]Full pipeline[/bold] — thread: {thread_id}")
    graph = await build_pipeline(thread_id)
    initial = empty_state()
    initial["search_params"] = SearchParams(query=query, location=location, remote=remote)
    config = {"configurable": {"thread_id": thread_id}}
    await graph.ainvoke(initial, config=config)
    console.print("[dim]Pipeline run complete (Phase 0 stub)[/dim]")


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
