"""command —— 节点对调度器说话的唯一控制词汇（对应第 06、09 课）。

一个节点执行完，产出的 Outcome 里可以带一条 control 命令，告诉调度器
“下一波该怎么走”。注意它只描述意图，真正改就绪集合的是调度器，
节点自己绝不直接去改别人的状态——控制权统一在引擎手里。

控制流只有这几条，没有第二种隐藏词汇：

- 不带命令（None）：沿 Plan 里画好的静态边自然往下走；
- Goto：把某个节点重新置为就绪，用来画“回边/跳转”（ReAct 的循环靠它）；
- Send：运行时动态实例化一个模板节点，用来扇出（数量运行时才知道）；
- Handoff：把控制权交给另一个 Agent（多 Agent 的 transfer 语义）。

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
    """下一波让 target 重新就绪（无论它之前是否完成）。"""

    target: str


@dataclass(frozen=True)
class Send(Command):
    """动态扇出：把模板节点 template 实例化跑一次，喂给它 payload。

    key 是实例标识，缺省时由调度器按序编号；同一 key 用于去重。
    """

    template: str
    payload: Any = None
    key: str | None = None


@dataclass(frozen=True)
class Handoff(Command):
    """交接：当前分支到此为止，换 agent 带着 task 继续（不回头）。"""

    agent: str
    task: str
