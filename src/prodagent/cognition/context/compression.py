from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from prodagent.cognition.context.budget import CompressionLevel, TokenCounter
from prodagent.cognition.context.spill import extract_spilled_path
from prodagent.core.types import StopReason
from prodagent.llm.base import stream_text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from prodagent.core.config import ContextConfig
    from prodagent.core.types import Message, MessageList
    from prodagent.llm.base import LLMClient

logger = logging.getLogger(__name__)

__all__ = [
    "Stage",
    "StageContext",
    "HistoryCompressor",
    "NoCompressionStage",
    "ToolCompressStage",
    "SummarizeStage",
    "EmergencyStage",
    "Summariser",
    "fit_budget",
    "safe_tail_start",
]

# File-scope so it's a stable Anthropic prompt-cache prefix.
_SUMMARISE_SYSTEM = (
    "You are a lossless context compressor for an AI agent. "
    "Return ONLY a JSON object - no prose, no markdown fences."
)

_SUMMARISE_SCHEMA = """\
Summarize the agent conversation below. Reply with ONLY this JSON (no other text):
{"focus": "<what the agent is working on now, <=15 words>", "done": ["<completed step>", "..."]}

Conversation turns:
"""

_TOPIC_SUMMARISE_SCHEMA = """\
Compress the conversation below to its essence. Reply with ONLY this JSON (no other text):
{"focus": "<the one thing the agent must accomplish next, <=20 words>", "key_result": "<single most important finding so far, <=25 words>"}

Conversation turns:
"""

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


def fit_budget(messages: MessageList, budget: int, counter: TokenCounter) -> MessageList:
    groups = _group_tool_pairs(messages)
    kept: list[list[Message]] = []
    used = 0
    for group in reversed(groups):
        group_tokens = sum(counter.count_message(m) for m in group)
        if used + group_tokens > budget:
            break
        kept.append(group)
        used += group_tokens
    kept.reverse()
    return [msg for group in kept for msg in group]


def safe_tail_start(messages: MessageList, recent_msgs: int) -> int:
    """Index of the verbatim 'recent' tail — walks back so it never lands on
    an orphan tool_result (which would split its pair from the assistant parent)."""
    idx = max(0, len(messages) - recent_msgs)
    while 0 < idx < len(messages) and "tool_call_id" in messages[idx]:
        idx -= 1
    return idx


def _group_tool_pairs(messages: MessageList) -> list[list[Message]]:
    """Atomic groups so tool_use / tool_result stay paired."""
    groups: list[list[Message]] = []
    open_tool_ids: set[str] = set()
    current_group: list[Message] | None = None

    for msg in messages:
        is_assistant_with_calls = msg.get("role") == "assistant" and bool(msg.get("tool_calls"))
        is_tool_result = "tool_call_id" in msg

        if is_assistant_with_calls:
            if current_group is not None:
                groups.append(current_group)
            current_group = [msg]
            open_tool_ids = {str(tc.get("id", "")) for tc in msg.get("tool_calls", [])}
        elif is_tool_result:
            call_id = str(msg.get("tool_call_id", ""))
            if current_group is not None and call_id in open_tool_ids:
                current_group.append(msg)
            else:
                if current_group is not None:
                    groups.append(current_group)
                    current_group = None
                    open_tool_ids = set()
                groups.append([msg])
        else:
            if current_group is not None:
                groups.append(current_group)
                current_group = None
                open_tool_ids = set()
            groups.append([msg])

    if current_group is not None:
        groups.append(current_group)
    return groups


class Summariser:
    """Compress a list of messages into a structured summary string via LLM."""

    def __init__(self, llm: LLMClient | None, cfg: ContextConfig) -> None:
        self._llm = llm
        self._cfg = cfg

    async def summarise(
        self, messages: MessageList, *, level: CompressionLevel = CompressionLevel.HISTORY_SUMMARY
    ) -> str:
        if not messages or self._llm is None:
            return ""
        return await self._summarise_with_llm(self._llm, messages, level=level)

    async def _summarise_with_llm(
        self, llm: LLMClient, messages: MessageList, *, level: CompressionLevel
    ) -> str:
        from prodagent.llm.base import LLMConfig

        cfg = self._cfg
        turns: list[str] = []
        for m in messages:
            role = m.get("role", "?")
            content = str(m.get("content", ""))[: cfg.summary_max_chars_per_turn]
            turns.append(f"{role.upper()}: {content}")
        turns_text = "\n---\n".join(turns)
        schema = (
            _TOPIC_SUMMARISE_SCHEMA
            if level == CompressionLevel.TOPIC_SUMMARY
            else _SUMMARISE_SCHEMA
        )
        prompt = schema + turns_text

        llm_cfg = LLMConfig(
            model=cfg.summary_model,
            max_tokens=cfg.summary_max_tokens,
            temperature=0.0,
            enable_prompt_caching=True,
        )

        try:
            response, raw_text = await stream_text(
                llm,
                [{"role": "user", "content": prompt}],
                system=_SUMMARISE_SYSTEM,
                config=llm_cfg,
            )
        except Exception as exc:
            logger.warning("LLM summarisation failed (%s); escalating to EmergencyStage", exc)
            return ""

        raw = raw_text.strip()
        if not raw:
            if (
                response.stop_reason == StopReason.MAX_TOKENS
                and response.output_tokens >= cfg.summary_max_tokens
            ):
                logger.warning(
                    "Summarisation hit max_tokens=%d with no content - raise summary_max_tokens",
                    cfg.summary_max_tokens,
                )
            else:
                logger.debug(
                    "Summarisation LLM returned empty content "
                    "(stop_reason=%r, input_tokens=%d, output_tokens=%d)",
                    response.stop_reason,
                    response.input_tokens,
                    response.output_tokens,
                )
            return ""
        return raw


@dataclass(frozen=True)
class StageContext:
    counter: TokenCounter
    config: ContextConfig
    summariser: Summariser


class Stage(Protocol):
    def should_run(self, ratio: float, cfg: ContextConfig) -> bool: ...

    async def apply(
        self, messages: MessageList, budget: int, ctx: StageContext
    ) -> tuple[MessageList, CompressionLevel]: ...


class HistoryCompressor:
    """Runs the first stage whose ``should_run`` matches."""

    def __init__(self, stages: list[Stage]) -> None:
        self._stages = stages

    async def run(
        self,
        messages: MessageList,
        budget: int,
        ctx: StageContext,
        ratio: float,
        *,
        max_level: CompressionLevel | None = None,
    ) -> tuple[MessageList, CompressionLevel]:
        chosen: Stage | None = None
        for stage in self._stages:
            if not stage.should_run(ratio, ctx.config):
                continue
            chosen = stage
            break
        if chosen is None:
            return fit_budget(messages, budget, ctx.counter), CompressionLevel.NONE

        if max_level is not None:
            chosen_level = _stage_level(chosen)
            if chosen_level is not None and chosen_level > max_level:
                # max_level caps escalation at the HIGHEST stage <= cap, not the
                # first stage <= cap (which is always NoCompressionStage).
                for stage in reversed(self._stages):
                    lvl = _stage_level(stage)
                    if lvl is not None and lvl <= max_level:
                        chosen = stage
                        break

        return await chosen.apply(messages, budget, ctx)


def _stage_level(stage: Stage) -> CompressionLevel | None:
    """Recover the CompressionLevel a stage reports, without running it."""
    attr = getattr(stage, "level", None)
    return attr if isinstance(attr, CompressionLevel) else None


class NoCompressionStage:
    level: CompressionLevel = CompressionLevel.NONE

    def should_run(self, ratio: float, cfg: ContextConfig) -> bool:
        return ratio < cfg.tool_compress_at

    async def apply(
        self, messages: MessageList, budget: int, ctx: StageContext
    ) -> tuple[MessageList, CompressionLevel]:
        return fit_budget(messages, budget, ctx.counter), CompressionLevel.NONE


class ToolCompressStage:
    """Oversized tool results are already spilled at append time; here we only
    fit the budget. The level is preserved so the pipeline still escalates
    NONE → TOOL_COMPRESS → summaries before EMERGENCY."""

    level: CompressionLevel = CompressionLevel.TOOL_COMPRESS

    def should_run(self, ratio: float, cfg: ContextConfig) -> bool:
        return ratio < cfg.history_summary_at

    async def apply(
        self, messages: MessageList, budget: int, ctx: StageContext
    ) -> tuple[MessageList, CompressionLevel]:
        return fit_budget(messages, budget, ctx.counter), CompressionLevel.TOOL_COMPRESS


@dataclass
class SummarizeStage:
    """Summarise older turns, keep a recent window verbatim."""

    recent_msgs: int
    level: CompressionLevel

    def should_run(self, ratio: float, cfg: ContextConfig) -> bool:
        if self.level == CompressionLevel.HISTORY_SUMMARY:
            return ratio < cfg.topic_summary_at
        return ratio < cfg.emergency_at

    async def apply(
        self, messages: MessageList, budget: int, ctx: StageContext
    ) -> tuple[MessageList, CompressionLevel]:
        idx = safe_tail_start(messages, self.recent_msgs)
        older = messages[:idx]
        recent = messages[idx:]
        summary = await ctx.summariser.summarise(older, level=self.level) if older else ""

        result: list[Message] = []
        if summary:
            label = (
                "TOPIC SUMMARY"
                if self.level == CompressionLevel.TOPIC_SUMMARY
                else "HISTORY SUMMARY"
            )
            result.append({"role": "user", "content": f"[{label}]\n{summary}"})
        result.extend(recent)
        return fit_budget(result, budget, ctx.counter), self.level


class EmergencyStage:
    """Keep last 2 messages + the most recent HISTORY SUMMARY, then fit."""

    level: CompressionLevel = CompressionLevel.EMERGENCY

    def should_run(self, ratio: float, cfg: ContextConfig) -> bool:
        return True

    async def apply(
        self, messages: MessageList, budget: int, ctx: StageContext
    ) -> tuple[MessageList, CompressionLevel]:
        emergency_msgs = list(messages[safe_tail_start(messages, 2) :])
        emergency_ids = {id(m) for m in emergency_msgs}

        summary_msg: Message | None = None
        for msg in reversed(messages):
            content = str(msg.get("content", ""))
            if (
                content.startswith("[HISTORY SUMMARY]") or content.startswith("[TOPIC SUMMARY]")
            ) and id(msg) not in emergency_ids:
                summary_msg = msg
                break

        if summary_msg is not None:
            emergency_msgs = [summary_msg, *emergency_msgs]

        actions_budget = min(_ACTIONS_TAKEN_MAX_TOKENS, max(0, budget))
        fitted = fit_budget(emergency_msgs, max(0, budget - actions_budget), ctx.counter)
        kept_ids = {id(m) for m in fitted}
        kept_tool_call_ids = {str(m.get("tool_call_id", "")) for m in fitted if "tool_call_id" in m}
        actions_body = _build_actions_taken(
            messages,
            kept_ids=kept_ids,
            kept_tool_call_ids=kept_tool_call_ids,
            counter=ctx.counter,
            max_tokens=actions_budget,
        )

        if actions_body:
            actions_msg: Message = {
                "role": "user",
                "content": f"[ACTIONS TAKEN]\n{actions_body}",
            }
            if summary_msg is not None and fitted and fitted[0] is summary_msg:
                fitted = [fitted[0], actions_msg, *fitted[1:]]
            else:
                fitted = [actions_msg, *fitted]

        return fitted, CompressionLevel.EMERGENCY
