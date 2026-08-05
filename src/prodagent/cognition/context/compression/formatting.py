from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from prodagent.cognition.context.spill import extract_spilled_path

if TYPE_CHECKING:
    from collections.abc import Iterator

    from prodagent.cognition.context.budget import TokenCounter
    from prodagent.core.types import MessageList

CHARS_PER_TOKEN = 4
_TOOL_CALL_ARG_CHARS = 48
_RESULT_HINT_CHARS = 60
_ACTIONS_TAKEN_MAX_TOKENS = 300
_RESULT_SIGNAL_KEYS = (
    "status",
    "ok",
    "error",
    "count",
    "total",
    "hits",
    "found",
    "passed",
    "failed",
    "result",
    "report",
    "url",
    "chars",
    "lines_total",
    "lines_shown",
)


def _truncate_str(s: str, max_chars: int) -> str:
    if len(s) <= max_chars:
        return s
    head = s[: max_chars // 2]
    tail = s[-(max_chars // 4) :]
    return f"{head} ...[{len(s) - len(head) - len(tail)} chars truncated]... {tail}"


def _format_tool_call(name: str, args: Any) -> str:
    if not isinstance(args, dict) or not args:
        return f"{name}()"
    parts: list[str] = []
    for k, v in list(args.items())[:3]:
        if isinstance(v, str):
            parts.append(f"{k}={v[:_TOOL_CALL_ARG_CHARS]!r}")
        elif isinstance(v, (int, float, bool)) or v is None:
            parts.append(f"{k}={v!r}")
        else:
            parts.append(f"{k}=<{type(v).__name__}>")
    return f"{name}({', '.join(parts)})"


def _extract_result_hint(content: str) -> str:
    if not content:
        return ""
    spilled_path = extract_spilled_path(content)
    if spilled_path:
        return f"spilled path={spilled_path!r}"
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        text = content.strip().replace("\n", " ")
        return _truncate_str(text, _RESULT_HINT_CHARS) if text else ""
    if isinstance(parsed, dict):
        parts: list[str] = []
        for key in _RESULT_SIGNAL_KEYS:
            if key in parsed:
                v = parsed[key]
                if isinstance(v, str):
                    parts.append(f"{key}={v[:_RESULT_HINT_CHARS]!r}")
                else:
                    parts.append(f"{key}={v!r}")
                if len(parts) >= 3:
                    break
        return ", ".join(parts)
    if isinstance(parsed, list):
        return f"list[{len(parsed)}]"
    return ""


def _extract_tc_fields(tc: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    """Recover (name, args, call_id) from either wire-shape or simplified tool_call."""
    func = tc.get("function")
    if isinstance(func, dict):
        name = str(func.get("name", "tool"))
        raw_args = func.get("arguments", "{}")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except (json.JSONDecodeError, TypeError):
            args = {}
    else:
        name = str(tc.get("name", "tool"))
        args = tc.get("args") or tc.get("params") or {}
    cid = str(tc.get("id", ""))
    return name, args if isinstance(args, dict) else {}, cid


def _iter_tool_pairs(
    messages: MessageList,
    *,
    kept_ids: set[int] | None = None,
    kept_tool_call_ids: set[str] | None = None,
) -> Iterator[tuple[str, dict[str, Any], str]]:
    """Walk ``messages`` and yield ``(name, args, result_hint)`` for each tool action."""
    pending: dict[str, tuple[str, dict[str, Any]]] = {}
    for m in messages:
        if m.get("role") == "assistant":
            if kept_ids is not None and id(m) in kept_ids:
                for tc in m.get("tool_calls") or []:
                    _, _, cid = _extract_tc_fields(tc)
                    if cid:
                        pending[cid] = ("", {})
                continue
            for tc in m.get("tool_calls") or []:
                name, args, cid = _extract_tc_fields(tc)
                if cid:
                    pending[cid] = (name, args)
                else:
                    yield name, args, ""
            continue
        if "tool_call_id" in m:
            cid = str(m.get("tool_call_id", ""))
            if kept_ids is not None and id(m) in kept_ids:
                continue
            if kept_tool_call_ids is not None and cid in kept_tool_call_ids:
                continue
            hint = _extract_result_hint(str(m.get("content", "")))
            if not hint:
                continue
            name, args = pending.pop(cid, ("", {}))
            yield name, args, hint


def _build_actions_taken(
    messages: MessageList,
    *,
    kept_ids: set[int],
    kept_tool_call_ids: set[str],
    counter: TokenCounter,
    max_tokens: int = _ACTIONS_TAKEN_MAX_TOKENS,
) -> str:
    """Compact ledger of every tool call in ``messages`` not already kept."""
    seen: dict[str, int] = {}
    for name, args, hint in _iter_tool_pairs(
        messages, kept_ids=kept_ids, kept_tool_call_ids=kept_tool_call_ids
    ):
        if name and hint:
            line = f"{_format_tool_call(name, args)} → {hint}"
        elif name:
            line = _format_tool_call(name, args)
        elif hint:
            line = f"result → {hint}"
        else:
            continue
        seen[line] = seen.get(line, 0) + 1

    if not seen:
        return ""

    lines = [line if count == 1 else f"{line}  (x{count})" for line, count in seen.items()]
    joined = "\n".join(lines)

    while counter.count(joined) > max_tokens and len(lines) > 1:
        lines.pop(0)
        joined = "\n".join(lines)
    if counter.count(joined) > max_tokens:
        joined = _truncate_str(joined, max_tokens * CHARS_PER_TOKEN)

    return joined
