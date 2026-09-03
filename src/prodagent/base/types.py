"""Shared vocabulary types — the bottom layer's words.

``Message`` / ``MessageList`` / ``RunState`` and
``stable_serialize`` are used by both ``base`` (session records) and
``kernel`` (the loop's state machine). They live here so the dependency
stays one-way: kernel imports base, base never imports kernel.
:mod:`prodagent.kernel.types` re-exports them unchanged.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, TypeAlias, TypedDict

#: Arbitrary JSON-serializable dict — use sparingly.
JsonDict: TypeAlias = dict[str, Any]

#: Tool parameter bag passed from LLM to tool function.
ToolParams: TypeAlias = dict[str, Any]

#: JSON Schema dict for a single tool's input schema.
ToolSchema: TypeAlias = dict[str, Any]

__all__ = [
    "JsonDict",
    "Message",
    "MessageList",
    "RunState",
    "ToolParams",
    "ToolSchema",
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
    """Best-effort stable JSON pre-serializer for fingerprint / hash computation.

    The codec's job is "reversible"; this one's is "comparable" — it renders
    datetimes/Decimals/paths into deterministic strings so semantically
    equal inputs always hash equal, the precondition dead-loop detection
    and cache keys rely on."""
    import datetime
    import decimal
    import pathlib

    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()  # canonical, timezone-explicit string form
    if isinstance(obj, datetime.timedelta):
        return repr(obj)  # isoformat doesn't exist for timedelta; repr is stable
    if isinstance(obj, decimal.Decimal):
        return str(obj)  # preserves exact digits — float() would round-trip badly
    if isinstance(obj, pathlib.PurePath):
        return str(obj)
    return repr(obj)  # last resort: repr is deterministic for most builtins


class RunState(StrEnum):
    RUNNING = "running"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    FAILED = "failed"
