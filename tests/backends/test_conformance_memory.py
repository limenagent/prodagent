"""Run the port conformance suite against every ``memory`` backend implementation."""

from __future__ import annotations

from prodagent.backends.conformance import (
    run_approval_conformance,
    run_approval_decision_flow_conformance,
    run_approval_decision_overwrite_conformance,
    run_approval_idempotent_create_conformance,
    run_cache_conformance,
    run_cache_key_isolation_conformance,
    run_dead_letter_conformance,
    run_dead_letter_escalation_conformance,
    run_dead_letter_message_isolation_conformance,
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
    run_vector_delete_conformance,
    run_vector_empty_search_conformance,
    run_vector_filter_conformance,
    run_vector_upsert_replaces_conformance,
    run_vector_upsert_search_conformance,
)
from prodagent.backends.memory import (
    InMemoryApprovalStore,
    InMemoryCache,
    InMemoryDeadLetterQueue,
    InMemoryGraphStore,
    InMemoryVectorStore,
    InProcessLockStore,
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


def _mem_vector():
    return lambda: InMemoryVectorStore()


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


# ── vector ────────────────────────────────────────────────────────────────────


def test_memory_vector_upsert_search_conformance():
    run_vector_upsert_search_conformance(_mem_vector())


def test_memory_vector_upsert_replaces_conformance():
    run_vector_upsert_replaces_conformance(_mem_vector())


def test_memory_vector_filter_conformance():
    run_vector_filter_conformance(_mem_vector())


def test_memory_vector_delete_conformance():
    run_vector_delete_conformance(_mem_vector())


def test_memory_vector_empty_search_conformance():
    run_vector_empty_search_conformance(_mem_vector())
