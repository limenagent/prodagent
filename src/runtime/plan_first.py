"""plan_first — recipe two: plan first, execute in parallel, then synthesize.

One key insight not to confuse: the "plan" produced by the LLM is just a step
list living in shared state (state.steps); it is **not** the kernel's execution
graph. The kernel graph always has only four fixed roles:

    planner ──Send fan-out──▶ worker* (template, one instance per step) ──join──▶ synth

The planner asks the model to break the task into N steps written to state and
Sends one worker instance per step; worker is a template node whose body can be
a plain function or a small tool-using ReAct; synth summarizes once all finish.
To re-plan, have a node Goto back to planner — no new engine needed.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from src.kernel import (
    FnBody,
    Goto,
    Node,
    Outcome,
    Plan,
    Send,
    last,
)

# make_steps: given the task and context, return [{"id":..., "instruction":...}, ...]
MakeSteps = Callable[[str, Any], Any]


def parse_numbered_list(text: str) -> list[dict]:
    """Parse model output like '1. xxx\\n2. yyy' into a step list (teaching parser)."""
    steps = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        head, _, rest = line.partition(".")
        if head.strip().isdigit() and rest.strip():
            steps.append({"id": f"s{head.strip()}", "instruction": rest.strip()})
    return steps


def build_plan_execute(*, make_steps: MakeSteps, worker: Any, synth: Any = None) -> Plan:
    """worker / synth are both NodeBody-compatible executables (usually wrapped in FnBody)."""

    async def planner(task, ctx):
        steps = await _maybe_await(make_steps(task, ctx))
        if not steps:
            return Outcome.goto("synth", steps=[])
        sends = [Send("worker", step, key=step["id"]) for step in steps]
        # Write the step list to state; fan out workers at the same time, and
        # re-arm the join point synth — so even on re-planning (some step Gotos
        # back to planner for another round), synth waits for this whole wave of
        # workers to finish instead of reusing the previous round's "done" state.
        return Outcome(
            state_delta={"steps": steps},
            # Goto.rejoin: only re-arm synth; it still waits for this wave of
            # workers to finish before synthesizing.
            control=[*sends, Goto.rejoin("synth")],
        )

    async def default_synth(inputs, ctx):
        # Outputs of the template predecessor worker are aggregated into a list.
        return Outcome.ok(inputs)

    plan = Plan(channels={"steps": last([])})
    plan.add(
        Node("planner", FnBody(planner)),
        Node("worker", worker, template=True),
        Node("synth", synth or FnBody(default_synth), terminal=True),
    )
    plan.edge("worker", "synth")
    plan.entry = ("planner",)
    return plan


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value
