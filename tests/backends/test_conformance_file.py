"""Run the port conformance suite against every ``file`` backend implementation.

File backends hold relational / durable state (checkpoint, event_log, document,
span) on a single host. Graph and vector data do not belong here — those have
their own dedicated engines (Neo4j) and their own conformance files.
"""

from __future__ import annotations

from prodagent.backends.file import (
    FileCheckpointStore,
    FileDocumentStore,
    FileEventLog,
    FileGraphStore,
    FileSpanExporter,
)
from tests.backends.conformance import (
    run_checkpoint_conformance,
    run_checkpoint_fork_conformance,
    run_checkpoint_fork_refuses_existing_conformance,
    run_checkpoint_versioning_conformance,
    run_document_conformance,
    run_document_constraint_storage_conformance,
    run_document_supersede_conformance,
    run_document_touch_conformance,
    run_event_log_batch_conformance,
    run_event_log_batch_expected_seq_conformance,
    run_event_log_conformance,
    run_event_log_empty_plan_conformance,
    run_event_log_plan_isolation_conformance,
    run_event_log_replicate_conformance,
    run_event_log_subscribe_conformance,
    run_graph_absent_node_neighbors_conformance,
    run_graph_delete_node_conformance,
    run_graph_edge_idempotent_conformance,
    run_graph_edge_neighbors_conformance,
    run_graph_list_nodes_conformance,
    run_graph_node_conformance,
    run_graph_node_merge_conformance,
    run_graph_traverse_depth_conformance,
    run_span_conformance,
    run_span_export_after_shutdown_conformance,
    run_span_shutdown_idempotent_conformance,
)


def _file_checkpoint(tmp_path):
    return lambda: FileCheckpointStore(tmp_path / "ckpt")


def _file_event_log(tmp_path):
    return lambda: FileEventLog(tmp_path / "evlog")


def _file_document(tmp_path):
    return lambda: FileDocumentStore(tmp_path / "docs")


def _file_graph(tmp_path):
    return lambda: FileGraphStore(tmp_path / "graph")


def _file_span(tmp_path):
    return lambda: FileSpanExporter(tmp_path / "trace.jsonl")


# ── checkpoint ────────────────────────────────────────────────────────────────


async def test_file_checkpoint_conformance(tmp_path):
    await run_checkpoint_conformance(_file_checkpoint(tmp_path))


async def test_file_checkpoint_versioning_conformance(tmp_path):
    await run_checkpoint_versioning_conformance(_file_checkpoint(tmp_path))


async def test_file_checkpoint_fork_conformance(tmp_path):
    await run_checkpoint_fork_conformance(_file_checkpoint(tmp_path))


async def test_file_checkpoint_fork_refuses_existing_conformance(tmp_path):
    await run_checkpoint_fork_refuses_existing_conformance(_file_checkpoint(tmp_path))


# ── event_log ─────────────────────────────────────────────────────────────────


async def test_file_event_log_conformance(tmp_path):
    await run_event_log_conformance(_file_event_log(tmp_path))


async def test_file_event_log_batch_conformance(tmp_path):
    await run_event_log_batch_conformance(_file_event_log(tmp_path))


async def test_file_event_log_batch_expected_seq_conformance(tmp_path):
    await run_event_log_batch_expected_seq_conformance(_file_event_log(tmp_path))


async def test_file_event_log_subscribe_conformance(tmp_path):
    await run_event_log_subscribe_conformance(_file_event_log(tmp_path))


async def test_file_event_log_plan_isolation_conformance(tmp_path):
    await run_event_log_plan_isolation_conformance(_file_event_log(tmp_path))


async def test_file_event_log_empty_plan_conformance(tmp_path):
    await run_event_log_empty_plan_conformance(_file_event_log(tmp_path))


# ── document ──────────────────────────────────────────────────────────────────


def test_file_document_conformance(tmp_path):
    run_document_conformance(_file_document(tmp_path))


def test_file_document_supersede_conformance(tmp_path):
    run_document_supersede_conformance(_file_document(tmp_path))


def test_file_document_touch_conformance(tmp_path):
    run_document_touch_conformance(_file_document(tmp_path))


def test_file_document_constraint_storage_conformance(tmp_path):
    run_document_constraint_storage_conformance(_file_document(tmp_path))


# ── graph ─────────────────────────────────────────────────────────────────────


def test_file_graph_node_conformance(tmp_path):
    run_graph_node_conformance(_file_graph(tmp_path))


def test_file_graph_node_merge_conformance(tmp_path):
    run_graph_node_merge_conformance(_file_graph(tmp_path))


def test_file_graph_edge_neighbors_conformance(tmp_path):
    run_graph_edge_neighbors_conformance(_file_graph(tmp_path))


def test_file_graph_edge_idempotent_conformance(tmp_path):
    run_graph_edge_idempotent_conformance(_file_graph(tmp_path))


def test_file_graph_traverse_depth_conformance(tmp_path):
    run_graph_traverse_depth_conformance(_file_graph(tmp_path))


def test_file_graph_delete_node_conformance(tmp_path):
    run_graph_delete_node_conformance(_file_graph(tmp_path))


def test_file_graph_absent_node_neighbors_conformance(tmp_path):
    run_graph_absent_node_neighbors_conformance(_file_graph(tmp_path))


def test_file_graph_list_nodes_conformance(tmp_path):
    run_graph_list_nodes_conformance(_file_graph(tmp_path))


# ── span ──────────────────────────────────────────────────────────────────────


async def test_file_span_conformance(tmp_path):
    await run_span_conformance(_file_span(tmp_path))


async def test_file_span_shutdown_idempotent_conformance(tmp_path):
    await run_span_shutdown_idempotent_conformance(_file_span(tmp_path))


async def test_file_span_export_after_shutdown_conformance(tmp_path):
    await run_span_export_after_shutdown_conformance(_file_span(tmp_path))


async def testfile_event_log_replicate_conformance(tmp_path):
    await run_event_log_replicate_conformance(_file_event_log(tmp_path))
