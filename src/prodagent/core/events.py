"""Streaming event model — the discriminated union yielded by Agent.stream()."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from prodagent.core.state.run import AgentRun
    from prodagent.core.types import RunId, ToolCall, ToolName

__all__ = [
    "AgentEvent",
    "ThinkTokenEvent",
    "ToolCallStartEvent",
    "ToolResultEvent",
    "StepStartedEvent",
    "StepCompletedEvent",
    "StepFailedEvent",
    "RunCompletedEvent",
    "RunFailedEvent",
    "RunSuspendedEvent",
]


@dataclass(frozen=True, slots=True)
class ThinkTokenEvent:
    token: str
    run_id: RunId


@dataclass(frozen=True, slots=True)
class ToolCallStartEvent:
    call: ToolCall
    run_id: RunId


@dataclass(frozen=True, slots=True)
class ToolResultEvent:
    name: ToolName
    result: object
    run_id: RunId


@dataclass(frozen=True, slots=True)
class StepStartedEvent:
    step_id: str
    action: ToolName
    run_id: RunId


@dataclass(frozen=True, slots=True)
class StepCompletedEvent:
    step_id: str
    action: ToolName
    result: object
    run_id: RunId


@dataclass(frozen=True, slots=True)
class StepFailedEvent:
    """Triggers replan."""

    step_id: str
    action: ToolName
    error: str
    run_id: RunId


@dataclass(frozen=True, slots=True)
class RunCompletedEvent:
    run: AgentRun


@dataclass(frozen=True, slots=True)
class RunFailedEvent:
    """Terminated due to budget, loop-detection, or abort."""

    run: AgentRun
    error: str


@dataclass(frozen=True, slots=True)
class RunSuspendedEvent:
    run: AgentRun


AgentEvent: TypeAlias = (
    ThinkTokenEvent
    | ToolCallStartEvent
    | ToolResultEvent
    | StepStartedEvent
    | StepCompletedEvent
    | StepFailedEvent
    | RunCompletedEvent
    | RunFailedEvent
    | RunSuspendedEvent
)
