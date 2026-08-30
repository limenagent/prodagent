"""Run the port conformance suite against every ``memory`` backend implementation."""

from __future__ import annotations

from typing import Any

from prodagent.backends.memory import (
    InMemoryApprovalStore,
    InMemoryCache,
    InMemoryDeadLetterQueue,
    InMemoryEventLog,
    InMemoryGraphStore,
    InProcessLockStore,
)
from tests.backends.conformance import (
    run_approval_conformance,
    run_approval_decision_flow_conformance,
    run_approval_decision_overwrite_conformance,
    run_approval_idempotent_create_conformance,
    run_cache_conformance,
    run_cache_key_isolation_conformance,
    run_dead_letter_conformance,
    run_dead_letter_escalation_conformance,
    run_dead_letter_message_isolation_conformance,
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
    run_lock_conformance,
    run_lock_mutual_exclusion_conformance,
    run_lock_nonblocking_tryacquire_conformance,
    run_lock_release_idempotent_conformance,
)


def _mem_cache():
    return lambda: InMemoryCache()


def _mem_approval():
    return lambda: InMemoryApprovalStore()


def _mem_lock():
    return lambda: InProcessLockStore()


def _mem_dead_letter():
    return lambda: InMemoryDeadLetterQueue(max_retries=3)


def _mem_graph():
    return lambda: InMemoryGraphStore()


# ── cache ─────────────────────────────────────────────────────────────────────


async def test_memory_cache_conformance():
    await run_cache_conformance(_mem_cache())


async def test_memory_cache_key_isolation_conformance():
    await run_cache_key_isolation_conformance(_mem_cache())


# ── approval ──────────────────────────────────────────────────────────────────


async def test_memory_approval_conformance():
    await run_approval_conformance(_mem_approval())


async def test_memory_approval_idempotent_create_conformance():
    await run_approval_idempotent_create_conformance(_mem_approval())


async def test_memory_approval_decision_flow_conformance():
    await run_approval_decision_flow_conformance(_mem_approval())


async def test_memory_approval_decision_overwrite_conformance():
    await run_approval_decision_overwrite_conformance(_mem_approval())


# ── lock ──────────────────────────────────────────────────────────────────────


async def test_memory_lock_conformance():
    await run_lock_conformance(_mem_lock())


async def test_memory_lock_mutual_exclusion_conformance():
    await run_lock_mutual_exclusion_conformance(_mem_lock())


async def test_memory_lock_release_idempotent_conformance():
    await run_lock_release_idempotent_conformance(_mem_lock())


async def test_memory_lock_nonblocking_tryacquire_conformance():
    await run_lock_nonblocking_tryacquire_conformance(_mem_lock())


# ── dead_letter ───────────────────────────────────────────────────────────────


async def test_memory_dead_letter_conformance():
    await run_dead_letter_conformance(_mem_dead_letter())


async def test_memory_dead_letter_escalation_conformance():
    await run_dead_letter_escalation_conformance(_mem_dead_letter())


async def test_memory_dead_letter_message_isolation_conformance():
    await run_dead_letter_message_isolation_conformance(_mem_dead_letter())


# ── graph ─────────────────────────────────────────────────────────────────────


def test_memory_graph_node_conformance():
    run_graph_node_conformance(_mem_graph())


def test_memory_graph_node_merge_conformance():
    run_graph_node_merge_conformance(_mem_graph())


def test_memory_graph_edge_neighbors_conformance():
    run_graph_edge_neighbors_conformance(_mem_graph())


def test_memory_graph_edge_idempotent_conformance():
    run_graph_edge_idempotent_conformance(_mem_graph())


def test_memory_graph_traverse_depth_conformance():
    run_graph_traverse_depth_conformance(_mem_graph())


def test_memory_graph_delete_node_conformance():
    run_graph_delete_node_conformance(_mem_graph())


def test_memory_graph_absent_node_neighbors_conformance():
    run_graph_absent_node_neighbors_conformance(_mem_graph())


def test_memory_graph_list_nodes_conformance():
    run_graph_list_nodes_conformance(_mem_graph())


def _memory_event_log() -> Any:
    return lambda: InMemoryEventLog()


async def test_memory_event_log_conformance():
    await run_event_log_conformance(_memory_event_log())


async def test_memory_event_log_batch_conformance():
    await run_event_log_batch_conformance(_memory_event_log())


async def test_memory_event_log_batch_expected_seq_conformance():
    await run_event_log_batch_expected_seq_conformance(_memory_event_log())


async def test_memory_event_log_plan_isolation_conformance():
    await run_event_log_plan_isolation_conformance(_memory_event_log())


async def test_memory_event_log_empty_plan_conformance():
    await run_event_log_empty_plan_conformance(_memory_event_log())


async def test_memory_event_log_subscribe_conformance():
    await run_event_log_subscribe_conformance(_memory_event_log())


async def testmemory_event_log_replicate_conformance():
    await run_event_log_replicate_conformance(_memory_event_log())
