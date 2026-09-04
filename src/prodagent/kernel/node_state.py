"""NodeRuntimeState — how far one run has gotten with one node.

A :class:`~prodagent.kernel.graph.Node` is static blueprint: what to run, what it
waits for. How far it got — status, output, attempts — belongs to the run
executing it, the same line Plan/Run itself splits along: one blueprint,
many executions, each carrying its own state.

Transitions go through one entry per target state (mark_running /
mark_completed / mark_failed / mark_skipped / reset_to_pending);
bare field writes are a review comment, not a pattern. The allowed-transition
table is the whole lifecycle — anything outside it raises, so an illegal
state is a loud error at the write site, never a silent surprise three
waves later. There is deliberately no SUSPENDED: waiting for the world is
the run's fact (:class:`Run.interrupt`), and a parked node stays RUNNING
until its redo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from prodagent.base.determinism import now_monotonic
from prodagent.kernel.types import NodeStatus


class NodeStateError(RuntimeError):
    """An illegal node-state transition — the source status can't go there."""


# The allowed-transition table: SKIPPED is reachable from almost anywhere
# (replans and downstream-quarantine scrap nodes through no fault of their
# own), and SKIPPED is terminal.
_ALLOWED: dict[NodeStatus, frozenset[NodeStatus]] = {
    NodeStatus.PENDING: frozenset({NodeStatus.RUNNING, NodeStatus.SKIPPED}),
    NodeStatus.RUNNING: frozenset(
        {
            NodeStatus.COMPLETED,
            NodeStatus.FAILED,
            NodeStatus.SKIPPED,
            NodeStatus.PENDING,  # crash/park reset: mid-flight state is unknown, redo
        }
    ),
    NodeStatus.COMPLETED: frozenset({NodeStatus.SKIPPED, NodeStatus.PENDING}),
    # PENDING-from-COMPLETED has exactly one door: the graph asking
    # for a redo. Side effects already fired stay fired — the redo is the
    # graph's decision, taken with that knowledge, not a stale resume.
    NodeStatus.FAILED: frozenset({NodeStatus.SKIPPED}),
    NodeStatus.SKIPPED: frozenset(),
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

    def mark_skipped(self) -> None:
        """Scrapped by a waived branch or downstream quarantine — neither ran
        nor failed."""
        self._transition(NodeStatus.SKIPPED)

    def reset_to_pending(self) -> None:
        """Back to never-run — crash recovery and a parked run's resume both
        land here (mid-flight state is unknowable; the redo re-executes the
        node, and an approval's staged call retries verbatim). Attempts and
        the crash scene clear; a redo starts its own attempt history."""
        self._transition(NodeStatus.PENDING)
        self.output_ref = None
        self.error = None
