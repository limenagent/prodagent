"""NodeRuntimeState — how far one run has gotten with one node.

A :class:`~prodagent.plan.dag.Node` is static blueprint: what to run, what it
waits for. How far it got — status, output, attempts — belongs to the run
executing it, the same line Plan/Run itself splits along: one blueprint,
many executions, each carrying its own state.

Transitions go through one entry per target state (mark_running /
mark_completed / mark_failed / mark_obsolete / suspend / reset_to_pending);
bare field writes are a review comment, not a pattern. The allowed-transition
table is the whole lifecycle — anything outside it raises, so an illegal
state is a loud error at the write site, never a silent surprise three
waves later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from prodagent.base.determinism import now_monotonic
from prodagent.kernel.types import NodeStatus


class NodeStateError(RuntimeError):
    """An illegal node-state transition — the source status can't go there."""


# The allowed-transition table: OBSOLETE is reachable from almost anywhere
# (replans and downstream-quarantine scrap nodes through no fault of their
# own), COMPLETED only ever leaves via OBSOLETE (a replaced completed node
# keeps its side effects but stops mattering), and OBSOLETE is terminal.
_ALLOWED: dict[NodeStatus, frozenset[NodeStatus]] = {
    NodeStatus.PENDING: frozenset({NodeStatus.RUNNING, NodeStatus.OBSOLETE}),
    NodeStatus.RUNNING: frozenset(
        {
            NodeStatus.COMPLETED,
            NodeStatus.FAILED,
            NodeStatus.SUSPENDED,
            NodeStatus.OBSOLETE,
            NodeStatus.PENDING,  # crash reset: mid-flight state is unknown, redo
        }
    ),
    NodeStatus.SUSPENDED: frozenset({NodeStatus.RUNNING, NodeStatus.PENDING, NodeStatus.OBSOLETE}),
    NodeStatus.COMPLETED: frozenset({NodeStatus.OBSOLETE, NodeStatus.PENDING}),
    # PENDING-from-COMPLETED has exactly one door: an explicit Goto asking
    # for a redo. Side effects already fired stay fired — the redo is the
    # graph's decision, taken with that knowledge, not a stale resume.
    NodeStatus.FAILED: frozenset({NodeStatus.OBSOLETE}),
    NodeStatus.OBSOLETE: frozenset(),
}


@dataclass
class NodeRuntimeState:
    """Per-run execution state of one node — the mutable half of a Node."""

    node_id: str
    status: NodeStatus = NodeStatus.PENDING
    output_ref: Any = None
    error: str | None = None
    attempts: int = 0
    started_at: float = 0.0
    completed_at: float = 0.0

    def _transition(self, target: NodeStatus) -> None:
        if target not in _ALLOWED[self.status]:
            raise NodeStateError(
                f"node {self.node_id!r}: {self.status.value} → {target.value} is not "
                "a legal transition"
            )
        self.status = target

    def mark_running(self) -> None:
        """Start (or retry) this node — the one place ``attempts`` advances."""
        self._transition(NodeStatus.RUNNING)
        self.attempts += 1
        self.started_at = now_monotonic()

    def mark_completed(self, output: Any) -> None:
        """Finish with a result — the output pairs with the transition."""
        self._transition(NodeStatus.COMPLETED)
        self.output_ref = output
        self.error = None
        self.completed_at = now_monotonic()

    def mark_failed(self, error: str | BaseException) -> None:
        """Finish with the crash scene — the error pairs with the transition."""
        self._transition(NodeStatus.FAILED)
        self.error = str(error)

    def mark_obsolete(self) -> None:
        """Scrapped by a replan or downstream quarantine — neither ran nor failed."""
        self._transition(NodeStatus.OBSOLETE)

    def suspend(self) -> None:
        """Paused awaiting the world (HITL decision); resume retries this node."""
        self._transition(NodeStatus.SUSPENDED)

    def reset_to_pending(self) -> None:
        """Back to never-run — crash recovery (mid-flight state is
        unknowable), requeue-after-suspension, and an explicit Goto redo
        all land here. Attempts and the crash scene clear; a redo starts
        its own attempt history."""
        self._transition(NodeStatus.PENDING)
        self.output_ref = None
        self.error = None
