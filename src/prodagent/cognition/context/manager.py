from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from prodagent.cognition.context.budget import (
    CompressionLevel,
    ContextBudget,
    Layer,
    TokenCounter,
    fit_within_budget,
)
from prodagent.cognition.context.compression import (
    CHARS_PER_TOKEN,
    EmergencyStage,
    HistoryCompressor,
    NoCompressionStage,
    StageContext,
    Summariser,
    SummarizeStage,
    ToolCompressStage,
    safe_tail_start,
)
from prodagent.core.config import ContextConfig
from prodagent.kernel.bus import Gate, HookEvent, InjectionPoint

if TYPE_CHECKING:
    from prodagent.cognition.context.spill import ToolResultSpillStore
    from prodagent.kernel.bus import HookRegistry
    from prodagent.kernel.state import AgentRun
    from prodagent.kernel.types import Message, MessageList
    from prodagent.llm import LLMClient

logger = logging.getLogger(__name__)

__all__ = ["ContextManager", "format_state"]


@dataclass
class _Sandwich:
    """The assembled context window's structural shape."""

    memory_msg: Message | None = None
    skills_msg: Message | None = None
    history: list[Message] = field(default_factory=list)
    state_msg: Message | None = None
    reminder_msg: Message | None = None

    def to_messages(self) -> MessageList:
        msgs: list[Message] = list(self.history)
        if self.memory_msg is not None:
            msgs.append(self.memory_msg)
        if self.skills_msg is not None:
            msgs.append(self.skills_msg)
        if self.state_msg is not None:
            msgs.append(self.state_msg)
        if self.reminder_msg is not None:
            msgs.append(self.reminder_msg)
        return msgs

    def stable_prefix_len(self) -> int:
        """Count of messages ahead of the cache boundary (history only)."""
        return len(self.history)


def format_state(run: AgentRun) -> str:
    return (
        f"Turn: {run.turn_count} | "
        f"State: {run.state.value} | "
        f"Failures: {run.tool_failures} | "
        f"Last action: {run.last_action or 'none'}"
    )


class ContextManager:
    """Assembles the context window for each LLM call."""

    def __init__(
        self,
        config: ContextConfig | None = None,
        *,
        system_prompt: str = "",
        constraint_reminder: str = "",
        llm: LLMClient | None = None,
        spill_store: ToolResultSpillStore | None = None,
        aux_llm: LLMClient | None = None,
    ) -> None:
        if config is None:
            config = ContextConfig()
        self._cfg = config
        self._max = config.max_tokens
        self._system = system_prompt
        self._reminder = constraint_reminder
        self._llm = llm
        # Background summariser LLM: separate from the main client so aux
        # calls never steal from (or pollute) a scripted trajectory.
        self._aux_llm = aux_llm
        self._spill_store = spill_store

        self._counter = TokenCounter()
        self._pipeline = self._build_pipeline(config)
        self._system_tokens = self._counter.count(system_prompt)
        self._cache_boundary_index: int | None = None

    @property
    def config(self) -> ContextConfig:
        return self._cfg

    @property
    def counter(self) -> TokenCounter:
        return self._counter

    @property
    def cache_boundary_index(self) -> int | None:
        """Index of the last history message (the cache-stable prefix)."""
        return self._cache_boundary_index

    @property
    def spill_store(self) -> ToolResultSpillStore | None:
        return self._spill_store

    @staticmethod
    def _build_pipeline(config: ContextConfig) -> HistoryCompressor:
        return HistoryCompressor(
            [
                NoCompressionStage(),
                ToolCompressStage(),
                SummarizeStage(
                    recent_msgs=config.history_recent_msgs,
                    level=CompressionLevel.HISTORY_SUMMARY,
                ),
                SummarizeStage(
                    recent_msgs=config.topic_recent_msgs,
                    level=CompressionLevel.TOPIC_SUMMARY,
                ),
                EmergencyStage(),
            ]
        )

    async def prepare(
        self,
        run: AgentRun,
        *,
        memory_snippets: list[str] | None = None,
        hooks: HookRegistry | None = None,
        invoked_skills: dict[str, str] | None = None,
    ) -> tuple[str, MessageList]:
        """Return ``(system_prompt, messages)`` ready for the LLM call.

        System prompt kept separate for Anthropic prompt-cache stability.
        """
        budget = ContextBudget(self._cfg, self._max)

        state_block, state_tokens = self._alloc_state_block(budget, run)
        memory_block, memory_tokens = await self._alloc_memory_block(
            budget, run, hooks, memory_snippets
        )

        skills_block = self._build_invoked_skills_block(invoked_skills or {})
        skills_tokens = self._counter.count(skills_block)

        pre_history_tokens = sum(self._counter.count_message(m) for m in run.messages)
        history, compression = await self._compress_history(
            run, state_tokens, memory_tokens, skills_tokens
        )

        sandwich = self._assemble_sandwich(state_block, memory_block, skills_block, history)
        l3_tokens = sum(self._counter.count_message(m) for m in sandwich.history)
        budget.alloc(Layer.L3, l3_tokens)

        messages, total = self._enforce_total_budget(sandwich, budget)

        stable_len = sandwich.stable_prefix_len()
        self._cache_boundary_index = stable_len - 1 if stable_len > 0 else None

        run.messages = list(sandwich.history)

        await self._fire_context_hooks(
            compression,
            total,
            messages,
            hooks,
            run=run,
            layer_tokens={
                Layer.L0.value: self._system_tokens,
                Layer.L1.value: state_tokens,
                Layer.L2.value: memory_tokens,
                Layer.L3.value: l3_tokens,
            },
            pre_history_tokens=pre_history_tokens,
            max_tokens=self._max,
        )

        logger.debug("Context assembled: %d tokens, compression=%s", total, compression.name)
        return self._system, messages

    def _alloc_state_block(self, budget: ContextBudget, run: AgentRun) -> tuple[str, int]:
        budget.alloc(Layer.L0, self._system_tokens)
        if budget.is_over(Layer.L0):
            logger.warning(
                "L0 system prompt %d tokens exceeds L0 quota %d (%.0f%% of window) — "
                "it will squeeze L3 history. Shorten the system prompt or raise l0_ratio.",
                budget.layer_spent(Layer.L0),
                budget.layer_budget(Layer.L0),
                self._cfg.l0_ratio * 100,
            )

        state_block = format_state(run)
        state_tokens = self._counter.count(state_block)
        budget.alloc(Layer.L1, state_tokens)
        if budget.is_over(Layer.L1):
            logger.warning(
                "L1 state block %d tokens exceeds L1 quota %d — check run state size.",
                budget.layer_spent(Layer.L1),
                budget.layer_budget(Layer.L1),
            )
        return state_block, state_tokens

    async def _alloc_memory_block(
        self,
        budget: ContextBudget,
        run: AgentRun,
        hooks: HookRegistry | None,
        memory_snippets: list[str] | None,
    ) -> tuple[str, int]:
        memory_snippets = await self._collect_memory(run, hooks, memory_snippets)
        memory_block = "\n".join(memory_snippets) if memory_snippets else ""
        memory_tokens = self._counter.count(memory_block)
        budget.alloc(Layer.L2, memory_tokens)

        if budget.is_over(Layer.L2):
            memory_snippets = self._prune_layer(
                memory_snippets or [], budget.layer_budget(Layer.L2), self._counter
            )
            memory_block = "\n".join(memory_snippets) if memory_snippets else ""
            memory_tokens = self._counter.count(memory_block)
            budget.alloc(Layer.L2, memory_tokens)

        return memory_block, memory_tokens

    def _assemble_sandwich(
        self,
        state_block: str,
        memory_block: str,
        skills_block: str,
        history: MessageList,
    ) -> _Sandwich:
        return _Sandwich(
            memory_msg={"role": "user", "content": f"[MEMORY]\n{memory_block}"}
            if memory_block
            else None,
            skills_msg={"role": "user", "content": skills_block} if skills_block else None,
            history=list(history),
            state_msg={"role": "user", "content": f"[STATE]\n{state_block}"}
            if state_block
            else None,
            reminder_msg={"role": "user", "content": self._reminder} if self._reminder else None,
        )

    def _enforce_total_budget(
        self, sandwich: _Sandwich, budget: ContextBudget
    ) -> tuple[MessageList, int]:
        messages = sandwich.to_messages()
        total = self._counter.count(self._system) + sum(
            self._counter.count_message(m) for m in messages
        )
        if total > self._max:
            logger.error(
                "Context overflow: %d > %d tokens - truncating to last 2 messages",
                total,
                self._max,
            )
            sandwich.history = list(sandwich.history[safe_tail_start(sandwich.history, 2) :])
            budget.alloc(
                Layer.L3,
                sum(self._counter.count_message(m) for m in sandwich.history),
            )
            messages = sandwich.to_messages()
            total = self._counter.count(self._system) + sum(
                self._counter.count_message(m) for m in messages
            )
        return messages, total

    def _build_invoked_skills_block(self, invoked_skills: dict[str, str]) -> str:
        if not invoked_skills:
            return ""
        per_skill = self._cfg.post_compact_max_tokens_per_skill
        total_budget = self._cfg.post_compact_skills_token_budget
        used = 0
        entries: list[str] = []
        for name, doc in invoked_skills.items():
            truncated = self._truncate_skill_doc(doc, per_skill)
            t = self._counter.count(truncated)
            if used + t > total_budget:
                break
            used += t
            entries.append(f"### Skill: {name}\n\n{truncated}")
        if not entries:
            return ""
        body = "\n\n---\n\n".join(entries)
        return (
            "[INVOKED SKILLS]\n"
            "The following skills were invoked in this session. "
            "Continue to follow these guidelines:\n\n" + body
        )

    def _truncate_skill_doc(self, doc: str, per_skill_tokens: int) -> str:
        char_budget = per_skill_tokens * CHARS_PER_TOKEN
        if len(doc) <= char_budget:
            return doc
        cut = doc.rfind("\n", 0, char_budget)
        if cut < char_budget // 2:
            cut = char_budget
        return doc[:cut] + "\n...[truncated]..."

    @staticmethod
    def _prune_layer(items: list[str], budget_tokens: int, counter: TokenCounter) -> list[str]:
        return fit_within_budget(
            items, budget_tokens, counter.count, separator_tokens=counter.count("\n")
        )

    async def _compress_history(
        self,
        run: AgentRun,
        state_tokens: int,
        memory_tokens: int,
        skills_tokens: int,
    ) -> tuple[MessageList, CompressionLevel]:
        current_usage = (
            self._system_tokens
            + state_tokens
            + memory_tokens
            + skills_tokens
            + sum(self._counter.count_message(m) for m in run.messages)
        )
        ratio = current_usage / self._max
        history_budget = (
            self._max
            - self._system_tokens
            - state_tokens
            - memory_tokens
            - skills_tokens
            - self._counter.count(self._reminder)
            - self._cfg.safety_margin
        )

        ctx = StageContext(
            counter=self._counter,
            config=self._cfg,
            summariser=Summariser(self._aux_llm or self._llm, self._cfg),
        )

        result, level = await self._pipeline.run(run.messages, history_budget, ctx, ratio)
        return result, level

    async def _fire_context_hooks(
        self,
        compression: CompressionLevel,
        total: int,
        messages: MessageList,
        hooks: HookRegistry | None,
        *,
        run: AgentRun | None = None,
        layer_tokens: dict[str, int] | None = None,
        pre_history_tokens: int = 0,
        max_tokens: int = 0,
    ) -> None:
        if not hooks:
            return
        run_id = run.run_id if run is not None else ""
        ctx_data = dict(
            system_tokens=self._system_tokens,
            msg_count=len(messages),
            compression=compression.name,
            total_tokens=total,
            messages=messages,
            spilled_results=(self._spill_store.spill_count if self._spill_store is not None else 0),
            layer_tokens=layer_tokens or {},
            pre_history_tokens=pre_history_tokens,
            max_tokens=max_tokens,
            run_id=run_id,
        )
        await hooks.check_blocking(Gate.CONTEXT_BUILD, **ctx_data)
        await hooks.fire(HookEvent.CONTEXT_BUILD, **ctx_data)

    async def _collect_memory(
        self,
        run: AgentRun,
        hooks: HookRegistry | None,
        memory_snippets: list[str] | None,
    ) -> list[str]:
        if not hooks:
            return list(memory_snippets) if memory_snippets else []

        if hooks.has_injector_handlers(InjectionPoint.CONTEXT_INJECTOR):
            extra = await hooks.collect(
                InjectionPoint.CONTEXT_INJECTOR,
                query=run.task,
            )
            new_strings = [s for s in extra if isinstance(s, str) and s]
            previews = [s[:80] + ("…" if len(s) > 80 else "") for s in new_strings[:3]]
            await hooks.fire(
                HookEvent.MEMORY_RECALL,
                query=run.task,
                hits=len(new_strings),
                previews=previews,
                run_id=run.run_id,
            )
            if new_strings:
                memory_snippets = (memory_snippets or []) + new_strings

        return list(memory_snippets) if memory_snippets else []
