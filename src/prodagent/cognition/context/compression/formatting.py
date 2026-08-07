from __future__ import annotations

import json
from typing import TYPE_CHECKING

from prodagent.cognition.context.spill import extract_spilled_path

if TYPE_CHECKING:
    from prodagent.core.config import ContextConfig

CHARS_PER_TOKEN = 4
_RESULT_HINT_CHARS = 60
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


def compress_tool_result(content: str, cfg: ContextConfig) -> str:
    """Collapse an oversized tool result's middle into a signal-field hint.

    Rule-based only (reuses ``_extract_result_hint``'s JSON-field extraction) —
    no LLM call. Already-spilled placeholders and short results pass through
    unchanged since there's nothing left worth shrinking.
    """
    if not content or extract_spilled_path(content) is not None:
        return content
    if len(content) <= cfg.inline_compress_min_chars:
        return content

    head = content[: cfg.inline_compress_head_chars]
    tail = content[-cfg.inline_compress_tail_chars :] if cfg.inline_compress_tail_chars else ""
    omitted = len(content) - len(head) - len(tail)
    hint = _extract_result_hint(content)
    hint_suffix = f" — {hint}" if hint else ""
    return f"{head}\n...[{omitted} chars omitted{hint_suffix}]...\n{tail}"
