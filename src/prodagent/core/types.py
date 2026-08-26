"""Shared vocabulary types — the bottom layer's words.

``Message`` / ``MessageList`` / ``RunState`` / ``ExecutionMode`` and
``stable_serialize`` are used by both ``core`` (session records, progress
fingerprints) and ``kernel`` (the loop's state machine). They live here so
the dependency stays one-way: kernel imports core, core never imports kernel.
:mod:`prodagent.kernel.types` re-exports them unchanged.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Literal, TypeAlias, TypedDict

if TYPE_CHECKING:
    from prodagent.core.aliases import JsonDict

__all__ = [
    "ExecutionMode",
    "Message",
    "MessageList",
    "RunState",
    "stable_serialize",
]


class Message(TypedDict, total=False):
    """Strongly-typed LLM conversation message."""

    role: Literal["user", "assistant", "system", "tool"]
    content: str | list[JsonDict]
    tool_calls: list[JsonDict]
    tool_call_id: str
    thinking: list[JsonDict]
    """Raw reasoning blocks carried on an assistant message (OpenAI-shaped
    wire has no seat for them; the Anthropic adapter re-emits them when a
    tool-use continuation must re-send the final assistant turn's thinking)."""


MessageList: TypeAlias = list[Message]


def stable_serialize(obj: object) -> object:
    """Best-effort stable JSON pre-serializer for fingerprint / hash computation."""
    import datetime
    import decimal
    import pathlib

    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()
    if isinstance(obj, datetime.timedelta):
        return repr(obj)
    if isinstance(obj, decimal.Decimal):
        return str(obj)
    if isinstance(obj, pathlib.PurePath):
        return str(obj)
    return repr(obj)


class RunState(StrEnum):
    RUNNING = "running"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    FAILED = "failed"


class ExecutionMode(StrEnum):
    """Controls how an Agent decides which tools to call and in what order."""

    PLAN_FIRST = "plan_first"
    """LLM proposes a full execution plan (JSON DAG); framework executes it.
    Enables plan auditing and HITL plan review. Opt-in (AgentConfig default
    is REACTIVE — the default path pays no planning tax)."""

    REACTIVE = "reactive"
    """Each turn picks the next tool based on the previous result.
    WARNING: bypasses plan auditing and HITL plan review. Default."""
