"""
Top-level pipeline graph.

Wires the four agent subgraphs into a single StateGraph with a shared
SqliteSaver checkpoint store. Each entry point gets its own thread_id,
which means stages can be entered independently without re-running prior stages.

Thread ID conventions:
  - Full run:    "run-{YYYY-MM-DD}-{uuid4[:8]}"
  - Discover:    "discover-{YYYY-MM-DD}"
  - Write:       "write-{job_id}"
  - Submit:      "submit-{job_id}"
  - Track:       "track-{YYYY-MM-DD}"
"""

from __future__ import annotations

from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, StateGraph

from pipeline.agents import (
    build_discoverer_graph,
    build_submitter_graph,
    build_tracker_graph,
    build_writer_graph,
)
from pipeline.config import settings
from pipeline.state import PipelineState


async def build_pipeline(thread_id: str):
    """
    Compile the top-level pipeline graph with a SqliteSaver checkpoint store.

    Returns a compiled graph ready for ainvoke() / astream().

    Usage:
        graph = await build_pipeline("run-2026-03-18-abc123")
        result = await graph.ainvoke(initial_state, config={"configurable": {"thread_id": thread_id}})
    """
    Path(settings.checkpoint_db_path).parent.mkdir(parents=True, exist_ok=True)

    checkpointer = AsyncSqliteSaver.from_conn_string(settings.checkpoint_db_path)

    graph = StateGraph(PipelineState)

    # Add each agent subgraph as a compiled node
    graph.add_node("discover", build_discoverer_graph().compile())
    graph.add_node("write", build_writer_graph().compile())
    graph.add_node("submit", build_submitter_graph().compile())
    graph.add_node("track", build_tracker_graph().compile())

    # Full pipeline: discover → write → submit; track is always available independently
    graph.set_entry_point("discover")
    graph.add_edge("discover", "write")
    graph.add_edge("write", "submit")
    graph.add_edge("submit", "track")
    graph.add_edge("track", END)

    return graph.compile(checkpointer=checkpointer)
