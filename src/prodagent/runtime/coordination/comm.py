"""Inter-agent communication primitives."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prodagent.core.budget import check_budget
from prodagent.core.exceptions import ContractViolationError

if TYPE_CHECKING:
    from prodagent.core.budget import HardBudget
    from prodagent.core.state.run import AgentRun
    from prodagent.core.types import ToolCall
    from prodagent.runtime.coordination.spawn import ChildResult

logger = logging.getLogger(__name__)

_DEFAULT_PRIOR_OUTPUT_MAX_CHARS = 2000


@dataclass
class HandoffPacket:
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_description: str = ""
    constraints: list[str] = field(default_factory=list)
    available_tools: list[str] = field(default_factory=list)
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    input_refs: dict[str, str] = field(default_factory=dict)
    prior_output: str = ""
    prior_output_max_chars: int = _DEFAULT_PRIOR_OUTPUT_MAX_CHARS

    def to_task_prompt(self) -> str:
        lines = [self.task_description.strip(), ""]
        if self.prior_output:
            trimmed = self.prior_output[: self.prior_output_max_chars]
            if len(self.prior_output) > self.prior_output_max_chars:
                trimmed += f"\n…(truncated, {len(self.prior_output) - self.prior_output_max_chars} more chars)"
            lines.append("Prior agent output:")
            lines.append(trimmed)
            lines.append("")
        if self.constraints:
            lines.append("Constraints:")
            lines.extend(f"  - {c}" for c in self.constraints)
        if self.available_tools:
            lines.append("Available tools:")
            lines.append("  - " + "\n  - ".join(self.available_tools))
        if self.input_refs:
            lines.append("Input references (resolve via tools, do not inline):")
            lines.extend(f"  - {name}: {handle}" for name, handle in self.input_refs.items())
        return "\n".join(lines)


@dataclass
class HandoffContract:
    required_fields: list[str]
    field_types: dict[str, type] = field(default_factory=dict)
    optional_fields: list[str] = field(default_factory=list)
    strict: bool = True

    def validate(self, result: dict[str, Any]) -> tuple[bool, str | None]:
        for name in self.required_fields:
            if name not in result:
                return False, f"missing required field {name!r}"
            expected = self.field_types.get(name)
            if expected is not None and not isinstance(result[name], expected):
                return (
                    False,
                    f"field {name!r} expected {expected.__name__}, got {type(result[name]).__name__}",
                )
        for name in self.optional_fields:
            if name in result:
                expected = self.field_types.get(name)
                if expected is not None and not isinstance(result[name], expected):
                    return (
                        False,
                        f"field {name!r} expected {expected.__name__}, got {type(result[name]).__name__}",
                    )
        return True, None


class HandoffInterceptor:
    """Welded into ``SpawnPipeline.spawn`` — runs on every child result."""

    def intercept(self, result: dict[str, Any], contract: HandoffContract) -> dict[str, Any]:
        allowed = set(contract.required_fields) | set(contract.optional_fields)
        filtered = {k: v for k, v in result.items() if k in allowed}

        ok, error = contract.validate(filtered)
        if not ok:
            raise ContractViolationError(
                f"SubAgent response violates the contract: {error}", field=error or ""
            )
        return filtered


class IdempotentMessageHandler:
    def __init__(self, ttl_seconds: float = 3600.0) -> None:
        self._seen: dict[str, float] = {}
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()

    async def is_duplicate(self, message_id: str) -> bool:
        async with self._lock:
            now = time.monotonic()
            cutoff = now - self._ttl
            if any(ts < cutoff for ts in self._seen.values()):
                self._seen = {mid: ts for mid, ts in self._seen.items() if ts >= cutoff}
            if message_id in self._seen:
                logger.debug("Duplicate message suppressed: %s", message_id)
                return True
            self._seen[message_id] = now
            return False


def check_spawn_budget(
    run: AgentRun,
    budget: HardBudget | None,
    accumulators: list[SpawnAccumulator],
) -> None:
    """Check budget, folding live sub-agent spend into the parent run totals."""
    if budget is None:
        return
    check_budget(
        run,
        budget,
        extra_turns=sum(a.turns for a in accumulators),
        extra_tokens=sum(a.input_tokens + a.output_tokens for a in accumulators),
        extra_cost_usd=sum(a.cost_usd for a in accumulators),
    )


def fold_spawn_fields(target: Any, source: Any) -> None:
    """Add source's flat spawn-accounting fields onto target, in place."""
    target.cost_usd += source.cost_usd
    target.input_tokens += source.input_tokens
    target.output_tokens += source.output_tokens
    if source.tool_history:
        target.tool_history.extend(source.tool_history)


def fold_spawn_accounting(run: Any, accumulator: SpawnAccumulator | None) -> None:
    """Fold an accumulator's totals onto a run — no-op if nothing was spawned."""
    if accumulator is None or accumulator.spawn_count == 0:
        return
    m = run.metrics
    m.cost_usd += accumulator.cost_usd
    m.input_tokens += accumulator.input_tokens
    m.output_tokens += accumulator.output_tokens
    m.turn_count += accumulator.turns
    if accumulator.tool_history:
        run.tool_history.extend(accumulator.tool_history)
    logger.debug(
        "[spawn] folded %d sub-agent spawns: +$%.4f, +%d turns, +%d tools",
        accumulator.spawn_count,
        accumulator.cost_usd,
        accumulator.turns,
        len(accumulator.tool_history),
    )


@dataclass
class SpawnAccumulator:
    """Shared sink for sub-agent accounting so parent runs can reconcile cost."""

    cost_usd: float = 0.0
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    spawn_count: int = 0
    tool_history: list[ToolCall] = field(default_factory=list)

    def add(self, result: ChildResult) -> None:
        fold_spawn_fields(self, result)
        self.turns += result.turns
        self.spawn_count += 1
