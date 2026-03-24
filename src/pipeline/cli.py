"""
CLI entry point.

All subcommands are wired. Phase 0 implementations are no-ops that confirm
the graph compiles and the DB migrates cleanly.

Usage (after `uv pip install -e .`):
  pipeline discover
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

console = Console()
app = typer.Typer(
    name="pipeline",
    help="LangGraph multi-agent job application pipeline.",
    add_completion=False,
)
db_app = typer.Typer(help="Database management commands.")
app.add_typer(db_app, name="db")


# ── Helpers ────────────────────────────────────────────────────────────────────


def run(coro):
    """Convenience wrapper to run an async function from a sync typer command."""
    return asyncio.run(coro)


# ── Discovery ──────────────────────────────────────────────────────────────────


@app.command()
def discover(
    query: str = typer.Option("software engineer", "--query", "-q", help="Job title or keyword"),
    location: str = typer.Option("Chicago, IL", "--location", "-l", help="Location filter"),
    remote: bool = typer.Option(True, "--remote/--no-remote", help="Include remote roles"),
):
    """Search all configured job sources and present a triage shortlist."""
    run(_discover(query, location, remote))


async def _discover(query: str, location: str, remote: bool):
    from pipeline.graph import build_pipeline
    from pipeline.state import SearchParams, empty_state

    thread_id = f"discover-{datetime.utcnow().date()}"
    console.print(f"[bold green]Discoverer[/bold green] — thread: {thread_id}")

    graph = await build_pipeline(thread_id)
    initial = empty_state()
    initial["search_params"] = SearchParams(query=query, location=location, remote=remote)

    config = {"configurable": {"thread_id": thread_id}}
    result = await graph.ainvoke(initial, config=config)
    console.print("[dim]Discovery run complete (Phase 0 stub — no results yet)[/dim]")


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
    console.print("[dim]Writer run complete (Phase 0 stub — no documents generated yet)[/dim]")


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
    console.print("[dim]Submitter run complete (Phase 0 stub — no form filled yet)[/dim]")


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
    console.print("[dim]Tracker run complete (Phase 0 stub — no dashboard yet)[/dim]")


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
