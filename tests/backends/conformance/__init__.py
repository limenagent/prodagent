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

from tests.backends.conformance.approval import (
    run_approval_conformance,
    run_approval_decision_flow_conformance,
    run_approval_decision_overwrite_conformance,
    run_approval_idempotent_create_conformance,
)
from tests.backends.conformance.cache import (
    run_cache_conformance,
    run_cache_key_isolation_conformance,
)
from tests.backends.conformance.checkpoint import (
    run_checkpoint_conformance,
    run_checkpoint_fork_conformance,
    run_checkpoint_fork_refuses_existing_conformance,
    run_checkpoint_versioning_conformance,
)
from tests.backends.conformance.dead_letter import (
    run_dead_letter_conformance,
    run_dead_letter_escalation_conformance,
    run_dead_letter_message_isolation_conformance,
)
from tests.backends.conformance.document import (
    run_document_conformance,
    run_document_constraint_storage_conformance,
    run_document_supersede_conformance,
    run_document_touch_conformance,
)
from tests.backends.conformance.event_log import (
    run_event_log_batch_conformance,
    run_event_log_batch_expected_seq_conformance,
    run_event_log_conformance,
    run_event_log_empty_plan_conformance,
    run_event_log_plan_isolation_conformance,
    run_event_log_subscribe_conformance,
)
from tests.backends.conformance.graph import (
    run_graph_absent_node_neighbors_conformance,
    run_graph_delete_node_conformance,
    run_graph_edge_idempotent_conformance,
    run_graph_edge_neighbors_conformance,
    run_graph_list_nodes_conformance,
    run_graph_node_conformance,
    run_graph_node_merge_conformance,
    run_graph_traverse_depth_conformance,
)
from tests.backends.conformance.lock import (
    run_lock_conformance,
    run_lock_mutual_exclusion_conformance,
    run_lock_nonblocking_tryacquire_conformance,
    run_lock_release_idempotent_conformance,
)
from tests.backends.conformance.span import (
    run_span_conformance,
    run_span_export_after_shutdown_conformance,
    run_span_shutdown_idempotent_conformance,
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
    "run_event_log_subscribe_conformance",
    "run_event_log_batch_expected_seq_conformance",
    "run_event_log_batch_conformance",
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
    "run_lock_conformance",
    "run_lock_mutual_exclusion_conformance",
    "run_lock_nonblocking_tryacquire_conformance",
    "run_lock_release_idempotent_conformance",
    "run_span_conformance",
    "run_span_export_after_shutdown_conformance",
    "run_span_shutdown_idempotent_conformance",
]
