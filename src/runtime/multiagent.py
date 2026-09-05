"""multiagent —— 配方三：多 Agent 协作，全部用同一套内核拼出来。

三种最常见的协作关系，没有一个需要新引擎：

- pipeline（流水线）：静态边把几个子 Agent 串起来，上游产出喂下游；
- supervisor（主管-工人，call/委派）：主管本身就是一个 ReAct，只不过它的
  “工具”是一个个子 Agent——调用工具 = 递归激活一个子 Run，跑完把结果交回，
  主管据此再决定下一步。这就是 ADK 的 agent-as-tool，用内核原语天然表达；
- transfer（接力/交接，不回头）：在同一张图里把各 Agent 当节点，用 go 转到目标
  Agent 节点、不画回边即一去不返，和 call 的“去了要回来”形成对照，无需专门命令。
另外提供 build_blackboard：多角色共写一块共享板、主持人 join=all 汇聚、可多轮趋同。
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

from src.kernel import (
    FnBody,
    Goto,
    Node,
    Outcome,
    Plan,
    Run,
    SubPlanBody,
    append,
    last,
)
from src.runtime.react import build_react_plan, start_react_run
from src.runtime.tools import ToolRegistry, ToolSpec

DEFAULT_SUPERVISOR_SYSTEM = (
    "你是主管，不亲自做具体执行。根据用户目标，选择合适的专业子 Agent 去完成；"
    "拿到它们的结果后，决定是否还需要再派谁，最终汇总成对用户的答复。"
)


def _as_body(spec: Any):
    """一张 Plan 用 SubPlanBody 递归激活；已经是 body 的原样使用。"""
    return SubPlanBody(spec) if isinstance(spec, Plan) else spec


# —— 流水线：静态串联 ——
def build_pipeline(stages: list[tuple[str, Any]]) -> Plan:
    """stages 为 [(名字, 子Plan或body), ...]，按顺序执行，末节点产出最终结果。"""
    plan = Plan()
    names = []
    for i, (name, spec) in enumerate(stages):
        is_last = i == len(stages) - 1
        plan.add(Node(name, _as_body(spec), terminal=is_last))
        names.append(name)
    for a, b in pairwise(names):
        plan.edge(a, b)
    plan.entry = (names[0],)
    return plan


# —— 主管-工人：子 Agent 即工具（call 语义）——
def register_agent_tool(
    registry: ToolRegistry, name: str, child_plan: Plan, description: str
) -> None:
    """把一个子 Agent 注册成主管可调用的“委派工具”。"""

    async def delegate(task: str, ctx: Any):
        result = await ctx.spawn(child_plan, task)  # 递归起子 Run，call 语义要返回
        return result["output"]

    registry.add(
        ToolSpec(
            name=name,
            description=description,
            func=delegate,
            parameters={
                "type": "object",
                "properties": {"task": {"type": "string", "description": "交给该子 Agent 的任务"}},
                "required": ["task"],
            },
            side_effect="read",
        )
    )


def build_supervisor(
    workers: dict[str, tuple[Plan, str]],
    *,
    system: str = "",
    context: Any = None,
    memory: Any = None,
    registry: ToolRegistry | None = None,
) -> Plan:
    """workers: {工具名: (子 Plan, 给主管看的能力说明)}。主管就是一个 ReAct。"""
    registry = registry or ToolRegistry()
    for name, (child_plan, desc) in workers.items():
        register_agent_tool(registry, name, child_plan, desc)
    return build_react_plan(
        registry, system=system or DEFAULT_SUPERVISOR_SYSTEM, context=context, memory=memory
    )


async def run_supervisor(plan: Plan, task: str, scheduler: Any) -> Run:
    run = start_react_run(plan, task)
    await scheduler.drive(plan, run)
    return run


# 说明：多 Agent 的“交接（transfer，不回头）”不需要专门控制器——在同一张
# Workflow 图里把各 Agent 当节点，用 go(目标Agent, 交接摘要) 转场、且不画回边，
# 控制权就一去不返；这与 call（ctx.spawn 子 Run、干完返回）正好对照。


# —— 黑板：共享工作区 + 多角色并行 + 主持人汇聚（可多轮趋同）——
def build_blackboard(
    experts: list[tuple[str, Any]],
    moderator: Any,
    *,
    final: Any = None,
    board_key: str = "board",
) -> Plan:
    """搭一块“共享黑板”：异构专家并行写、主持人按 join=all 汇聚裁决。

    结构（全部是已有原语，没有为黑板新造引擎）：

        fanout ──并行──▶ expert1 ┐
                 ├──────▶ expert2 ├──▶ moderator(join=all) ──共识──▶ final
                 └──────▶ expert3 ┘            │ 未达成
                                             └─ Goto 回 fanout 再来一轮

    - experts 是 [(名字, body 或子 Plan), ...]，每个专家是不同角色（不同提示/工具），
      它们只往共享通道 board_key（append）追加自己的意见，彼此不直接通信；
    - moderator 是主持人 body，读 ctx.shared 裁决：达成则 Outcome.goto("final",
      verdict=...)，未达成则 Outcome.goto("fanout", round=r+1) 触发下一轮；
    - 多轮的关键是 fanout 每轮用 Goto(节点, immediate=False) 把专家和主持人“重新武装”：
      专家等 fanout 完成即并行，主持人依旧等所有专家这一轮齐活才裁决。
    """
    expert_names: list[str] = []

    async def fanout(_, ctx):
        # 只重新武装、不立即激活：并行时机仍由 fanout→expert 的边、汇聚时机
        # 仍由 expert→moderator 的 join=all 决定，让这套判定每一轮都重来。
        return Outcome(control=[Goto(n, immediate=False) for n in (*expert_names, "moderator")])

    plan = Plan(channels={board_key: append(), "round": last(0), "verdict": last(None)})
    plan.add(Node("fanout", FnBody(fanout)))
    for name, body in experts:
        plan.add(Node(name, _as_body(body)))
        plan.edge("fanout", name)
        plan.edge(name, "moderator")
        expert_names.append(name)
    plan.add(Node("moderator", _as_body(moderator), join="all"))

    async def default_final(_, ctx):
        return Outcome.ok({"verdict": ctx.shared.get("verdict"), board_key: ctx.shared[board_key]})

    plan.add(
        Node(
            "final", _as_body(final) if final is not None else FnBody(default_final), terminal=True
        )
    )
    plan.entry = ("fanout",)
    return plan
