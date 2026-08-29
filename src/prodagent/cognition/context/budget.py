"""Context budgeting — the token arithmetic compression answers to.

Two ideas: the window is *layered* (L0 system / L1 state / L2 memory / L3
history, each with a configured share), and counting is deliberately
approximate — a CJK-aware estimator that overestimates, because a gate that
trips early costs a little compression while a gate that trips late costs
an overflowed request. ``fit_within_budget`` keeps the most-recent tail:
when history must shrink, the oldest turns go first."""

from __future__ import annotations

from enum import Enum, StrEnum
from typing import TYPE_CHECKING, TypeVar

from prodagent.base.text import cjk_char_count

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from prodagent.base.config import ContextConfig
    from prodagent.kernel.types import Message


class Layer(StrEnum):
    """Context-window layering — L0 system / L1 state / L2 memory / L3 history.

    Cognition vocabulary: the layers exist because the compressor budgets per
    slice. Lived in kernel/types for historical reasons; its only consumers
    have always been here and manager.py."""

    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


__all__ = [
    "Layer",
    "TokenCounter",
    "CompressionLevel",
    "ContextBudget",
    "BudgetTracker",
    "fit_within_budget",
]

T = TypeVar("T")


class CompressionLevel(int, Enum):
    NONE = 0
    TOOL_COMPRESS = 1
    HISTORY_SUMMARY = 2
    TOPIC_SUMMARY = 3
    EMERGENCY = 4


class TokenCounter:
    """CJK-aware byte estimator — intentionally overestimates so the gate trips early."""

    def count(self, text: str) -> int:
        if not text:
            return 0
        cjk = cjk_char_count(text)
        ascii_chars = len(text) - cjk
        # Rough per-token sizes: ~1.5 chars per CJK token, ~4 per ASCII word
        # — deliberately overestimating so the compression gate trips early
        # rather than after the request already overflowed.
        return max(1, int(cjk / 1.5) + ascii_chars // 4)

    def count_message(self, msg: Message) -> int:
        content = msg.get("content", "")
        if isinstance(content, list):
            total = 0
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text") or block.get("content")
                    if text is None:
                        text = str(block)
                else:
                    text = str(block)
                total += self.count(str(text))
            return total
        return self.count(str(content))


class ContextBudget:
    """Per-layer spend against per-layer shares of one window. ``spent()``
    excludes the constraint reminder and safety margin — the compressor's
    trigger reads actual payload size, not padded size."""

    def __init__(self, config: ContextConfig, max_tokens: int) -> None:
        self._cfg = config
        self._max = max_tokens
        self._ratios: dict[Layer, float] = {
            Layer.L0: config.l0_ratio,
            Layer.L1: config.l1_ratio,
            Layer.L2: config.l2_ratio,
            Layer.L3: config.l3_ratio,
        }
        self._spent: dict[Layer, int] = dict.fromkeys(Layer, 0)

    def alloc(self, layer: Layer, tokens: int) -> None:
        self._spent[layer] = tokens

    def spent(self) -> int:
        # Excludes the constraint reminder and safety margin — basis for
        # compression-level detection.
        return sum(self._spent.values())

    def remaining(self) -> int:
        return self._max - self.spent()

    def layer_spent(self, layer: Layer) -> int:
        return self._spent[layer]

    def layer_budget(self, layer: Layer) -> int:
        return int(self._max * self._ratios[layer])

    def is_over(self, layer: Layer) -> bool:
        return self.layer_spent(layer) > self.layer_budget(layer)

    def breakdown(self) -> dict[str, int]:
        return {layer.value: self._spent[layer] for layer in Layer} | {"free": self.remaining()}


class BudgetTracker:
    """Tracks a shrinking token budget for greedy selection loops."""

    def __init__(self, budget: int) -> None:
        self._remaining = budget

    def try_take(self, tokens: int) -> bool:
        if tokens > self._remaining:
            return False
        self._remaining -= tokens
        return True


def fit_within_budget(
    items: Sequence[T],
    budget: int,
    token_of: Callable[[T], int],
    *,
    separator_tokens: int = 0,
) -> list[T]:
    """Keep the longest tail of ``items`` (most recent last) that fits ``budget``."""
    tracker = BudgetTracker(budget)
    kept: list[T] = []
    for item in reversed(items):
        cost = token_of(item) + (separator_tokens if kept else 0)
        if not tracker.try_take(cost):
            break
        kept.append(item)
    kept.reverse()
    return kept
