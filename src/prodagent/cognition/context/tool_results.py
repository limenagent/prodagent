from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from prodagent.cognition.context.spill import ToolResultSpillStore
    from prodagent.core.config import ContextConfig
    from prodagent.core.types import Message, ToolCall

logger = logging.getLogger(__name__)

__all__ = ["reduce_on_append"]

_SPILL_MARKER = "<spilled"


def _expand_string_newlines(serialized: str) -> str:
    return serialized.replace("\\n", "\n")


def _pretty_json(text: str) -> str:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text
    return _expand_string_newlines(json.dumps(parsed, indent=2, ensure_ascii=False))


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
            return _expand_string_newlines(json.dumps(result_wire, indent=2, ensure_ascii=False))
        except (TypeError, ValueError):
            return str(result_wire)

    if isinstance(result_wire, (list, tuple)):
        try:
            return _expand_string_newlines(json.dumps(result_wire, indent=2, ensure_ascii=False))
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
        return msg
    content = str(msg.get("content", ""))
    if not content or _SPILL_MARKER in content:
        return msg
    if len(content) <= max_result_chars:
        return msg
    spilled = spill_store.spill(content=content, call_id=call_id, tool_name=tool_name)
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
        "content": _serialize_for_spill(result_wire),
    }
    if cfg is None:
        return msg
    return _spill_if_oversized(
        msg,
        call.call_id,
        call.name,
        max_result_chars,
        cfg.spill_preview_chars,
        spill_store,
    )
