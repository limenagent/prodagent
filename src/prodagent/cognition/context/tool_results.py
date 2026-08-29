"""Tool-result reduction — the single throat results pass on append.

Every tool result entering a transcript funnels through
:func:`reduce_on_append` (the dispatcher's and the plan executor's shared
path): under the spill threshold it passes through; past it, the payload
moves to the spill store and the message keeps a bounded preview plus the
path ``read_tool_result`` can page back through. Serialization here is
human-shaped on disk (pretty JSON, real newlines) because the spill file's
reader is a grepping model, not a parser."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from prodagent.base.config import ContextConfig
    from prodagent.cognition.context.spill import ToolResultSpillStore
    from prodagent.kernel.types import Message, ToolCall

logger = logging.getLogger(__name__)

__all__ = ["reduce_on_append"]

_SPILL_MARKER = "<spilled"


def _expand_string_newlines(serialized: str) -> str:
    """Turn escaped ``\\n`` into real newlines for human-readable spill files.

    Only applied to content written to disk (so ``read_tool_result`` grep sees
    logical lines). In-context message content keeps ``\\n`` escaped so the
    JSON stays valid for downstream parsers (e.g. ``_extract_result_hint``).
    """
    return serialized.replace("\\n", "\n")


def _pretty_json(text: str) -> str:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text
    return json.dumps(parsed, indent=2, ensure_ascii=False)


def _serialize_for_spill(result_wire: Any) -> str:
    if isinstance(result_wire, str):
        return _pretty_json(result_wire)

    if isinstance(result_wire, dict):
        val = result_wire.get("result")
        if isinstance(val, str):
            pretty = _pretty_json(val)
            if pretty != val:
                return pretty
        content = result_wire.get("content")
        if isinstance(content, list):
            texts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(_pretty_json(block.get("text", "")))
            if texts:
                return "\n".join(texts)
        try:
            return json.dumps(result_wire, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(result_wire)

    if isinstance(result_wire, (list, tuple)):
        try:
            return json.dumps(result_wire, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(result_wire)

    return str(result_wire)


def _spill_if_oversized(
    msg: Message,
    call_id: str,
    tool_name: str,
    max_result_chars: float,
    preview_chars: int,
    spill_store: ToolResultSpillStore | None,
) -> Message:
    if spill_store is None or max_result_chars == float("inf"):
        return msg  # spill off or explicitly unbounded (read_tool_result pages itself)
    content = str(msg.get("content", ""))
    if not content or _SPILL_MARKER in content:
        return msg  # already a placeholder — never spill a spill
    if len(content) <= max_result_chars:
        return msg  # under the threshold: verbatim, zero machinery
    # Over the line: payload to disk, bounded preview + handle into context.
    spilled = spill_store.spill(
        content=_expand_string_newlines(content), call_id=call_id, tool_name=tool_name
    )
    return {**msg, "content": spilled.placeholder(preview_chars)}


def reduce_on_append(
    result_wire: dict[str, Any],
    call: ToolCall,
    cfg: ContextConfig,
    spill_store: ToolResultSpillStore | None = None,
    *,
    max_result_chars: float = 100_000,
) -> Message:
    msg: Message = {
        "role": "tool",
        "tool_call_id": call.call_id,
        "content": _serialize_for_spill(result_wire),  # human-shaped: readable if it does spill
    }
    if cfg is None:
        return msg  # no context config → no spill threshold → verbatim append
    return _spill_if_oversized(
        msg,
        call.call_id,
        call.name,
        max_result_chars,
        cfg.spill_preview_chars,
        spill_store,
    )
