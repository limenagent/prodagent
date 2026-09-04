"""types —— 全内核共享的最底层值对象与枚举。

它们有两个共同点：不可变、不依赖内核里任何其它模块。
正因为是叶子，谁都可以安全地 import 它，而不会产生循环依赖。
（状态用枚举显式表达，不让非法状态有机会出现。）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class NodeStatus(StrEnum):
    """图里一个节点（的一次执行实例）所处的状态。"""

    PENDING = "pending"  # 还没轮到它
    RUNNING = "running"  # 这一波正在跑
    COMPLETED = "completed"  # 成功完成，输出已落袋
    SKIPPED = "skipped"  # 条件边全部不满足，结构性跳过
    FAILED = "failed"  # 执行失败


class RunState(StrEnum):
    """一次 Run 的一生只有四个状态。"""

    RUNNING = "running"
    SUSPENDED = "suspended"  # 主动放手、落盘等人/等外部
    COMPLETED = "completed"
    FAILED = "failed"


# 状态机里唯一合法的转移表。除此之外的跳转一律报错——
# “让非法状态根本造不出来”，比事后判断当前是什么状态可靠得多。
_ALLOWED_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.RUNNING: frozenset({RunState.SUSPENDED, RunState.COMPLETED, RunState.FAILED}),
    RunState.SUSPENDED: frozenset({RunState.RUNNING, RunState.FAILED}),
    RunState.COMPLETED: frozenset(),
    RunState.FAILED: frozenset(),
}


@dataclass(frozen=True)
class ToolCall:
    """一次工具调用请求。

    call_id 是稳定的幂等键：同一次决策重试时它不变，工具侧据此去重，
    保证“至少投递一次 + 幂等 = 效果恰好一次”。
    """

    name: str
    arguments: dict = field(default_factory=dict)
    call_id: str = ""


@dataclass(frozen=True)
class ToolResult:
    """一次工具调用的结果。ok 为 False 时 error 说明原因。"""

    ok: bool
    output: object = None
    error: str = ""
    call_id: str = ""

    @classmethod
    def success(cls, output: object = None, call_id: str = "") -> ToolResult:
        return cls(True, output=output, call_id=call_id)

    @classmethod
    def failure(cls, error: str, call_id: str = "") -> ToolResult:
        return cls(False, error=error, call_id=call_id)
