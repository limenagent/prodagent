from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from prodagent.cognition.context.budget import CompressionLevel, TokenCounter, fit_within_budget
from prodagent.cognition.context.compression.formatting import compress_tool_result

if TYPE_CHECKING:
    from prodagent.base.config import ContextConfig
    from prodagent.cognition.context.compression.summarizer import Summariser
    from prodagent.kernel.types import Message, MessageList

__all__ = [
    "Stage",
    "StageContext",
    "HistoryCompressor",
    "NoCompressionStage",
    "ToolCompressStage",
    "SummarizeStage",
    "EmergencyStage",
    "fit_budget",
    "safe_tail_start",
]


def fit_budget(messages: MessageList, budget: int, counter: TokenCounter) -> MessageList:
    groups = _group_tool_pairs(messages)
    kept = fit_within_budget(
        groups, budget, lambda group: sum(counter.count_message(m) for m in group)
    )
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


@dataclass(frozen=True)
class StageContext:
    counter: TokenCounter
    config: ContextConfig
    summariser: Summariser


class Stage(Protocol):
    def should_skip(self, ratio: float, cfg: ContextConfig) -> bool: ...

    async def apply(
        self, messages: MessageList, budget: int, ctx: StageContext
    ) -> tuple[MessageList, CompressionLevel]: ...


class HistoryCompressor:
    """Runs the first stage whose ``should_skip`` returns False.

    Stages are ordered least → most aggressive. ``should_skip`` returns True
    when ``ratio`` has outgrown that stage and the compressor should escalate.
    The final stage caps the chain by always returning False.
    """

    def __init__(self, stages: list[Stage]) -> None:
        self._stages = stages

    async def run(
        self,
        messages: MessageList,
        budget: int,
        ctx: StageContext,
        ratio: float,
    ) -> tuple[MessageList, CompressionLevel]:
        chosen: Stage | None = None
        for stage in self._stages:
            if stage.should_skip(ratio, ctx.config):
                continue
            chosen = stage
            break
        if chosen is None:
            return fit_budget(messages, budget, ctx.counter), CompressionLevel.NONE

        return await chosen.apply(messages, budget, ctx)


class NoCompressionStage:
    level: CompressionLevel = CompressionLevel.NONE

    def should_skip(self, ratio: float, cfg: ContextConfig) -> bool:
        return ratio >= cfg.tool_compress_at

    async def apply(
        self, messages: MessageList, budget: int, ctx: StageContext
    ) -> tuple[MessageList, CompressionLevel]:
        return fit_budget(messages, budget, ctx.counter), CompressionLevel.NONE


class ToolCompressStage:
    """Collapse oversized tool results in place (rule-based signal-field hint,
    no LLM call) before fitting the budget. Already-spilled results are
    untouched — they're already down to a preview."""

    level: CompressionLevel = CompressionLevel.TOOL_COMPRESS

    def should_skip(self, ratio: float, cfg: ContextConfig) -> bool:
        return ratio >= cfg.history_summary_at

    async def apply(
        self, messages: MessageList, budget: int, ctx: StageContext
    ) -> tuple[MessageList, CompressionLevel]:
        compressed: MessageList = [
            {**m, "content": compress_tool_result(str(m.get("content", "")), ctx.config)}
            if "tool_call_id" in m
            else m
            for m in messages
        ]
        return fit_budget(compressed, budget, ctx.counter), CompressionLevel.TOOL_COMPRESS


@dataclass
class SummarizeStage:
    """Summarise older turns, keep a recent window verbatim."""

    recent_msgs: int
    level: CompressionLevel

    def should_skip(self, ratio: float, cfg: ContextConfig) -> bool:
        if self.level == CompressionLevel.HISTORY_SUMMARY:
            return ratio >= cfg.topic_summary_at
        return ratio >= cfg.emergency_at

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

    def should_skip(self, ratio: float, cfg: ContextConfig) -> bool:
        return False

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

        fitted = fit_budget(emergency_msgs, max(0, budget), ctx.counter)
        return fitted, CompressionLevel.EMERGENCY
