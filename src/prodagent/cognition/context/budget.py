from __future__ import annotations

from enum import Enum, StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prodagent.core.config import ContextConfig
    from prodagent.core.types import Message

__all__ = ["Layer", "TokenCounter", "CompressionLevel", "ContextBudget"]


class Layer(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


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
        from prodagent.core.text import cjk_char_count

        cjk = cjk_char_count(text)
        ascii_chars = len(text) - cjk
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
