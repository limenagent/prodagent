"""types — leaf value objects and enums shared by the whole kernel.

They share two properties: immutable, and independent of every other kernel
module. Being leaves, anyone can import them safely without creating a cycle.
(States are expressed as explicit enums so that invalid states cannot exist.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class NodeStatus(StrEnum):
    """Status of one (execution instance of a) node in the graph."""

    PENDING = "pending"  # not eligible yet
    RUNNING = "running"  # running in the current wave
    COMPLETED = "completed"  # finished successfully, output is settled
    SKIPPED = "skipped"  # no conditional edge matched, structurally skipped
    FAILED = "failed"  # execution failed


class RunState(StrEnum):
    """A Run has exactly four states in its lifetime."""

    RUNNING = "running"
    SUSPENDED = "suspended"  # deliberately released and persisted, waiting on a human/external
    COMPLETED = "completed"
    FAILED = "failed"


# The only legal transition table. Any jump outside of it raises —
# "make invalid states unrepresentable" is far more reliable than checking
# the current state after the fact.
_ALLOWED_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.RUNNING: frozenset({RunState.SUSPENDED, RunState.COMPLETED, RunState.FAILED}),
    RunState.SUSPENDED: frozenset({RunState.RUNNING, RunState.FAILED}),
    RunState.COMPLETED: frozenset(),
    RunState.FAILED: frozenset(),
}


@dataclass(frozen=True)
class ToolCall:
    """A single tool-call request.

    ``call_id`` is unique per call within the Run (the call counter); it is NOT
    stable across retries or suspension — redelivery can re-execute a side
    effect. A stable key needs a persisted call cursor, out of scope here.
    """

    name: str
    arguments: dict = field(default_factory=dict)
    call_id: str = ""


@dataclass(frozen=True)
class ToolResult:
    """The result of a tool call. When ``ok`` is False, ``error`` says why."""

    ok: bool
    output: object = None
    error: str = ""
    call_id: str = ""

    @classmethod
    def success(cls, output: object = None, call_id: str = "") -> ToolResult:
        return cls(True, output=output, call_id=call_id)

    @classmethod
    def failure(cls, error: str, call_id: str = "") -> ToolResult:
        return cls(False, error=error, call_id=call_id)
