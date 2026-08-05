"""奶茶代购 demo 的 memory 工厂。

预置两条 constraint(预算上限 200、起送 5 杯),让 agent 提方案时自动遵守。
用户偏好(糖度/冰度/常点饮品)由 memory classify 从对话里提取,不在这里 seed。
"""

from __future__ import annotations

from prodagent.cognition.memory import MemoryManager, build_memory_manager
from prodagent.core.config import FrameworkConfig

_CONSTRAINTS = [
    "预算上限 200 元,任何订单总价不得超过 200 元。",
    "起送 5 杯,任何订单杯数不得少于 5 杯。",
]


def build_memory(
    *,
    framework_config: FrameworkConfig | None = None,
) -> MemoryManager:
    """建带预置 constraint 的 MemoryManager。

    Args:
        framework_config: 父 fw。不传时用默认。
    """
    fw = framework_config or FrameworkConfig.default()
    return build_memory_manager(
        framework_config=fw,
        constraints=list(_CONSTRAINTS),
    )
