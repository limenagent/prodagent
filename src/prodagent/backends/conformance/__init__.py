"""Port-level conformance tests for ``prodagent.backends`` implementations.

Each module exports ``run_<port>_*`` functions — plain functions (not pytest
tests) that assert the store obeys its port contract. Test files under
``tests/backends/`` parametrize these over every backend implementation.
When ``backends/redis`` and ``backends/postgres`` land (Steps 5–6), they
get the same conformance run for free by adding one entry to the parametrize
list in a sibling test file.

Why plain functions, not pytest fixtures: conformance is a property of the
*backend*, not of a test session. Plain functions let us call them from
pytest, from a CLI smoke check, or from a backend's own test suite without
duplicating the assertions.

Each ``store_factory`` is a zero-arg callable returning a fresh, empty
store instance. Backends that need a directory take ``tmp_path`` via
closure in the test file — the conformance function itself is path-agnostic.
"""

from __future__ import annotations

from prodagent.backends.conformance.approval import (
    run_approval_conformance,
    run_approval_decision_flow_conformance,
    run_approval_decision_overwrite_conformance,
    run_approval_idempotent_create_conformance,
)
from prodagent.backends.conformance.cache import (
    run_cache_conformance,
    run_cache_key_isolation_conformance,
)
from prodagent.backends.conformance.checkpoint import (
    run_checkpoint_conformance,
    run_checkpoint_fork_conformance,
    run_checkpoint_fork_refuses_existing_conformance,
    run_checkpoint_versioning_conformance,
)
from prodagent.backends.conformance.dead_letter import (
    run_dead_letter_conformance,
    run_dead_letter_escalation_conformance,
    run_dead_letter_message_isolation_conformance,
)
from prodagent.backends.conformance.document import (
    run_document_conformance,
    run_document_constraint_storage_conformance,
    run_document_supersede_conformance,
    run_document_touch_conformance,
)
from prodagent.backends.conformance.event_log import (
    run_event_log_conformance,
    run_event_log_empty_plan_conformance,
    run_event_log_plan_isolation_conformance,
)
from prodagent.backends.conformance.graph import (
    run_graph_absent_node_neighbors_conformance,
    run_graph_delete_node_conformance,
    run_graph_edge_idempotent_conformance,
    run_graph_edge_neighbors_conformance,
    run_graph_list_nodes_conformance,
    run_graph_node_conformance,
    run_graph_node_merge_conformance,
    run_graph_traverse_depth_conformance,
)
from prodagent.backends.conformance.idempotency import (
    run_idempotency_concurrent_conformance,
    run_idempotency_conformance,
    run_idempotency_key_isolation_conformance,
)
from prodagent.backends.conformance.lock import (
    run_lock_conformance,
    run_lock_mutual_exclusion_conformance,
    run_lock_release_idempotent_conformance,
)
from prodagent.backends.conformance.span import (
    run_span_conformance,
    run_span_export_after_shutdown_conformance,
    run_span_shutdown_idempotent_conformance,
)
from prodagent.backends.conformance.vector import (
    run_vector_delete_conformance,
    run_vector_empty_search_conformance,
    run_vector_filter_conformance,
    run_vector_upsert_replaces_conformance,
    run_vector_upsert_search_conformance,
)

__all__ = [
    "run_approval_conformance",
    "run_approval_decision_flow_conformance",
    "run_approval_decision_overwrite_conformance",
    "run_approval_idempotent_create_conformance",
    "run_cache_conformance",
    "run_cache_key_isolation_conformance",
    "run_checkpoint_conformance",
    "run_checkpoint_fork_conformance",
    "run_checkpoint_fork_refuses_existing_conformance",
    "run_checkpoint_versioning_conformance",
    "run_dead_letter_conformance",
    "run_dead_letter_escalation_conformance",
    "run_dead_letter_message_isolation_conformance",
    "run_document_conformance",
    "run_document_constraint_storage_conformance",
    "run_document_supersede_conformance",
    "run_document_touch_conformance",
    "run_event_log_conformance",
    "run_event_log_empty_plan_conformance",
    "run_event_log_plan_isolation_conformance",
    "run_graph_absent_node_neighbors_conformance",
    "run_graph_delete_node_conformance",
    "run_graph_edge_idempotent_conformance",
    "run_graph_edge_neighbors_conformance",
    "run_graph_list_nodes_conformance",
    "run_graph_node_conformance",
    "run_graph_node_merge_conformance",
    "run_graph_traverse_depth_conformance",
    "run_idempotency_conformance",
    "run_idempotency_concurrent_conformance",
    "run_idempotency_key_isolation_conformance",
    "run_lock_conformance",
    "run_lock_mutual_exclusion_conformance",
    "run_lock_release_idempotent_conformance",
    "run_span_conformance",
    "run_span_export_after_shutdown_conformance",
    "run_span_shutdown_idempotent_conformance",
    "run_vector_delete_conformance",
    "run_vector_empty_search_conformance",
    "run_vector_filter_conformance",
    "run_vector_upsert_replaces_conformance",
    "run_vector_upsert_search_conformance",
]
