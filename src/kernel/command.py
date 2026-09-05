"""command —— 节点对调度器说话的唯一控制词汇。

一个节点执行完，产出的 Outcome 里可以带一条 control 命令，告诉调度器
“下一波该怎么走”。注意它只描述意图，真正改就绪集合的是调度器，
节点自己绝不直接去改别人的状态——控制权统一在引擎手里。

控制流只有这几条，没有第二种隐藏词汇：

- 不带命令（None）：沿 Plan 里画好的静态边自然往下走；
- Goto：重新武装某个节点，用来画“回边/跳转”（ReAct 的循环靠它），可带 payload
  作为目标下一次的输入；
- Send：运行时动态实例化一个模板节点，用来扇出（数量运行时才知道）。

多 Agent 的“交接（transfer）”不是新命令：在同一张图里 Goto 到另一个 Agent
节点、且不画回边，就是交出去不回头；交接摘要走 Goto 的 payload。

为什么没有 Update 命令？因为“往状态里写数据”是 Outcome.state_delta
这个一等字段自带的职责，不必再包一条命令。数据归数据、控制归控制。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Command:
    """控制命令基类；frozen 保证命令是不可变的事实。"""


@dataclass(frozen=True)
class Goto(Command):
    """把 target 重新武装（退回 PENDING），让它能再跑一次。

    重新武装之后“何时就绪”有两档，由 immediate 决定：
    - immediate=True（默认）：同时放进放行名单，下一波绕过前驱、立即就绪，
      用于顺序回边（ReAct 的循环）和运行时跳转；
    - immediate=False：只重新武装，不立即放行，是否就绪仍由入边和 join 判定，
      用于迭代汇聚点（多轮黑板、重规划后的汇总）——等这一波前驱重新齐活再跑，
      避免和前驱同一波抢跑、读到空结果。

    payload 是转场时喂给目标这一次的输入，和 Send 的 payload 对称：静态边沿
    上游 output 传值，动态转场靠命令 payload 传值（多 Agent 交接就用它带摘要）。
    """

    target: str
    immediate: bool = True
    payload: Any = None


@dataclass(frozen=True)
class Send(Command):
    """动态扇出：把模板节点 template 实例化跑一次，喂给它 payload。

    key 是实例标识，缺省时由调度器按序编号；同一 key 用于去重。
    """

    template: str
    payload: Any = None
    key: str | None = None
