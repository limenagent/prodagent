"""multiagent —— 配方三：多 Agent 协作，全部用同一套内核拼出来（第 26–31 课）。

三种最常见的协作关系，没有一个需要新引擎：

- pipeline（流水线）：静态边把几个子 Agent 串起来，上游产出喂下游；
- supervisor（主管-工人，call/委派）：主管本身就是一个 ReAct，只不过它的
  “工具”是一个个子 Agent——调用工具 = 递归激活一个子 Run，跑完把结果交回，
  主管据此再决定下一步。这就是 ADK 的 agent-as-tool，用内核原语天然表达；
- handoff（接力/transfer）：控制权交出去不回头，走内核的 Handoff 命令 +
  注入的 on_handoff 控制器，和 call 的“去了要回来”形成对照。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.kernel import (
    Node,
    Outcome,
    Plan,
    Run,
    SubPlanBody,
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
    for a, b in zip(names, names[1:]):
        plan.edge(a, b)
    plan.entry = (names[0],)
    return plan


# —— 主管-工人：子 Agent 即工具（call 语义）——
def register_agent_tool(registry: ToolRegistry, name: str, child_plan: Plan,
                        description: str) -> None:
    """把一个子 Agent 注册成主管可调用的“委派工具”。"""

    async def delegate(task: str, ctx: Any):
        result = await ctx.spawn(child_plan, task)     # 递归起子 Run，call 语义要返回
        return result["output"]

    registry.add(ToolSpec(
        name=name,
        description=description,
        func=delegate,
        parameters={"type": "object",
                    "properties": {"task": {"type": "string", "description": "交给该子 Agent 的任务"}},
                    "required": ["task"]},
        side_effect="read",
    ))


def build_supervisor(workers: dict[str, tuple[Plan, str]], *, system: str = "",
                     context: Any = None, memory: Any = None,
                     registry: ToolRegistry | None = None) -> Plan:
    """workers: {工具名: (子 Plan, 给主管看的能力说明)}。主管就是一个 ReAct。"""
    registry = registry or ToolRegistry()
    for name, (child_plan, desc) in workers.items():
        register_agent_tool(registry, name, child_plan, desc)
    return build_react_plan(
        registry, system=system or DEFAULT_SUPERVISOR_SYSTEM,
        context=context, memory=memory)


async def run_supervisor(plan: Plan, task: str, scheduler: Any) -> Run:
    run = start_react_run(plan, task)
    await scheduler.drive(plan, run)
    return run


# —— 接力：transfer 不回头（对照 call）——
@dataclass
class HandoffController:
    """作为 Scheduler(on_handoff=...) 注入：收到 Handoff 就激活目标 Agent 接力。

    与 call 的区别在于：交接不把结果交回调用者，控制权沿链向后传，chain 记录
    完整交接路径，便于审计。真实 swarm 里，模型产出“交给谁”的意图后映射成
    Outcome.handoff(...)，剩下的都由这个控制器统一处理。
    """

    agents: dict[str, Plan]
    chain: list[dict] = field(default_factory=list)

    async def __call__(self, cmd: Any, run: Run, scheduler: Any) -> None:
        if cmd.agent not in self.agents:
            raise RuntimeError(f"未知的交接目标：{cmd.agent}")
        self.chain.append({"from_run": run.run_id, "to": cmd.agent, "task": cmd.task})
        target = self.agents[cmd.agent]
        child = Run.start(target, parent_id=run.run_id, depth=run.depth + 1, task=cmd.task)
        await scheduler.drive(target, child)
        # transfer：把接力到的最终结果直接落到当前 Run，不回退给上一个调用者。
        run.complete(child.final_output)
