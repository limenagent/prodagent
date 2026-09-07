"""multiagent — recipe three: multi-agent collaboration, all assembled from the same kernel.

None of the three most common collaboration relationships needs a new engine:

- pipeline: static edges chain several sub-agents, feeding upstream output
  downstream;
- supervisor (call/delegation): the supervisor is itself a ReAct whose "tools"
  are sub-agents — calling a tool = recursively activating a child Run, which
  returns its result when done, and the supervisor decides the next step. This is
  ADK's agent-as-tool, expressed naturally with kernel primitives;
- transfer (handoff, no return): in one graph, treat agents as nodes and go to
  the target agent node without a return edge, so control leaves for good — in
  deliberate contrast to call's "go and come back". No dedicated command needed.
Also provides build_blackboard: several roles co-write one shared board, a
moderator joins with join=all, and they may converge over multiple rounds.
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
    "You are a supervisor and do not perform concrete execution yourself. "
    "Given the user's goal, choose the appropriate specialist sub-agent to do it; "
    "after receiving their results, decide whether to delegate to anyone else, and "
    "finally synthesize the answer to the user."
)


def _as_body(spec: Any):
    """A Plan is activated recursively via SubPlanBody; an existing body is used as-is."""
    return SubPlanBody(spec) if isinstance(spec, Plan) else spec


# ---- pipeline: static chaining ----
def build_pipeline(stages: list[tuple[str, Any]]) -> Plan:
    """stages is [(name, sub-Plan or body), ...], run in order; the last node yields the result."""
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


# ---- supervisor-worker: a sub-agent is a tool (call semantics) ----
def register_agent_tool(
    registry: ToolRegistry, name: str, child_plan: Plan, description: str
) -> None:
    """Register one sub-agent as a "delegation tool" the supervisor can call."""

    async def delegate(task: str, ctx: Any):
        result = await ctx.spawn(
            child_plan, task
        )  # recursively start a child Run; call means it returns
        return result["output"]

    registry.add(
        ToolSpec(
            name=name,
            description=description,
            func=delegate,
            parameters={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "The task to hand to this sub-agent"}
                },
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
    """workers: {tool name: (child Plan, capability description for the supervisor)}. The supervisor is a ReAct."""
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


# Note: multi-agent "transfer" (handoff, no return) needs no dedicated controller —
# in one Workflow graph, treat agents as nodes and use go(target_agent, handoff
# summary) without a return edge, so control leaves for good; this is the exact
# counterpart of call (ctx.spawn a child Run, return when done).


# ---- blackboard: shared workspace + parallel roles + moderator join (multi-round convergence) ----
def build_blackboard(
    experts: list[tuple[str, Any]],
    moderator: Any,
    *,
    final: Any = None,
    board_key: str = "board",
) -> Plan:
    """Build a "shared blackboard": heterogeneous experts write in parallel and a moderator adjudicates with join=all.

    Structure (all existing primitives; no new engine for the blackboard):

        fanout ──parallel──▶ expert1 ┐
                     ├──────▶ expert2 ├──▶ moderator(join=all) ──consensus──▶ final
                     └──────▶ expert3 ┘            │ not reached
                                                  └─ Goto back to fanout for another round

    - experts is [(name, body or sub-Plan), ...]; each expert is a different role
      (different prompt/tools), appending only its opinion to the shared append
      channel board_key, never talking to each other directly;
    - moderator is a body that reads ctx.shared to adjudicate: on consensus it
      Outcome.goto("final", verdict=...), otherwise Outcome.goto("fanout",
      round=r+1) to trigger the next round;
    - multi-round works because fanout each round uses Goto.rejoin(node) to
      "re-arm" experts and moderator: experts run in parallel once fanout
      completes, and the moderator still waits for every expert of that round.
    """
    expert_names: list[str] = []

    async def fanout(_, ctx):
        # Re-arm without immediate activation: parallel timing is still set by
        # the fanout→expert edges, and join timing by expert→moderator join=all,
        # so this judgment repeats every round.
        return Outcome(control=[Goto.rejoin(n) for n in (*expert_names, "moderator")])

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
