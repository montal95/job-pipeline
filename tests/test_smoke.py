"""
Smoke tests — verify all four agent graphs compile without errors.

These are the lightest possible integration checks: they confirm that graph
topology, node wiring, and conditional edges are internally consistent.
No nodes are executed; just graph.compile() is called.

Run these first in CI — a compile failure means nothing else will work.
"""

from __future__ import annotations


def test_discoverer_graph_compiles():
    from pipeline.agents.discoverer import build_discoverer_graph
    assert build_discoverer_graph().compile() is not None


def test_writer_graph_compiles():
    from pipeline.agents.writer import build_writer_graph
    assert build_writer_graph().compile() is not None


def test_submitter_graph_compiles():
    from pipeline.agents.submitter import build_submitter_graph
    assert build_submitter_graph().compile() is not None


def test_tracker_graph_compiles():
    from pipeline.agents.tracker import build_tracker_graph
    assert build_tracker_graph().compile() is not None
