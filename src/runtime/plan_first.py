"""plan_first —— 配方二：先规划、再并行执行、最后综合（第 20 课）。

这里有一个关键认知，千万别搞混：LLM 产出的“计划”只是共享状态里的一份步骤
清单（state.steps），**不是**内核那张执行图。内核图始终只有固定四个角色：

    planner ──Send 扇出──▶ worker*(模板，每步一个实例) ──汇聚──▶ synth

planner 让模型把任务拆成 N 步写进 state，并对每一步 Send 一个 worker 实例；
worker 是模板节点，可以是普通函数，也可以是一个会用工具的小 ReAct；全部完成
后 synth 汇总。想重规划，让某个节点 Goto 回 planner 即可，不需要新引擎。
"""

from __future__ import annotations

from typing import Any, Callable

from src.kernel import (
    FnBody,
    Node,
    Outcome,
    Plan,
    Run,
    Send,
    last,
)

# make_steps：拿到任务与上下文，返回 [{"id":..., "instruction":...}, ...]
MakeSteps = Callable[[str, Any], Any]


def parse_numbered_list(text: str) -> list[dict]:
    """把 '1. xxx\\n2. yyy' 这样的模型输出解析成步骤清单（教学版解析）。"""
    steps = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        head, _, rest = line.partition(".")
        if head.strip().isdigit() and rest.strip():
            steps.append({"id": f"s{head.strip()}", "instruction": rest.strip()})
    return steps


def build_plan_execute(*, make_steps: MakeSteps, worker: Any,
                       synth: Any = None) -> Plan:
    """worker / synth 都是满足 NodeBody 的执行体（通常直接用 FnBody 包一层）。"""

    async def planner(task, ctx):
        steps = await _maybe_await(make_steps(task, ctx))
        if not steps:
            return Outcome.goto("synth", steps=[])
        sends = [Send("worker", step, key=step["id"]) for step in steps]
        # 计划清单写进 state，同时把每个步骤扇出成一个 worker 实例。
        return Outcome.fan_out(*sends, steps=steps)

    async def default_synth(inputs, ctx):
        # template 前驱 worker 的输出会被聚合成一个 list 传进来。
        return Outcome.ok(inputs)

    plan = Plan(channels={"steps": last([])})
    plan.add(Node("planner", FnBody(planner)),
             Node("worker", worker, template=True),
             Node("synth", synth or FnBody(default_synth), terminal=True))
    plan.edge("worker", "synth")
    plan.entry = ("planner",)
    return plan


def start_plan_run(plan: Plan, task: str) -> Run:
    return Run.start(plan, task=task)


async def _maybe_await(value):
    import inspect
    if inspect.isawaitable(value):
        return await value
    return value
