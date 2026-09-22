"""Multi-agent: a sub-agent is a child Run recursively driven by some body, using the very same kernel."""

from src.kernel import (
    FnBody,
    Node,
    Outcome,
    Plan,
    Run,
    RunState,
    Scheduler,
    SubPlanBody,
    ToolCall,
    append,
)
from src.runtime.agent import Agent
from src.runtime.llm import ScriptedLlm
from src.runtime.tools import ToolSpec

_TASK_SCHEMA = {
    "type": "object",
    "properties": {"task": {"type": "string"}},
    "required": ["task"],
}


def child_plan():
    c = Plan(channels={"trace": append()})
    c.add(Node("work", FnBody(lambda x, ctx: Outcome.ok("child-result", trace=[x])), terminal=True))
    return c


async def test_subrun_call_returns_output_to_parent():
    parent = Plan()
    parent.add(Node("delegate", SubPlanBody(child_plan()), terminal=True))
    sch = Scheduler()
    started = []
    sch.bus.on("run_started", lambda evt: started.append(evt))
    run = await sch.run(parent, task="给子Agent的任务")
    assert run.state == RunState.COMPLETED
    # call semantics: by default only the child Run's final output is handed back to the parent node.
    assert run.final_output == "child-result"
    # the child Run's run_started event carries parent_id, so the Run tree can be rebuilt.
    child_starts = [e for e in started if e.parent_id == run.run_id]
    assert len(child_starts) == 1


async def test_nested_subruns_build_a_run_tree():
    # grandparent -> parent (body is a child Run) -> child, only output flows back up each level.
    grandchild = child_plan()
    middle = Plan()
    middle.add(Node("m", SubPlanBody(grandchild), terminal=True))
    root = Plan()
    root.add(Node("r", SubPlanBody(middle), terminal=True))
    run = await Scheduler().run(root, task="deep")
    assert run.state == RunState.COMPLETED
    assert run.final_output == "child-result"


async def test_mutual_delegation_loop_is_capped():
    # A activates B and B activates A: without a depth guard this recurses
    # forever, so it must fail at the Run-tree depth limit (enforced at birth).
    plan_a, plan_b = Plan(), Plan()
    plan_a.add(Node("a", SubPlanBody(plan_b), terminal=True))
    plan_b.add(Node("b", SubPlanBody(plan_a), terminal=True))
    run = await Scheduler().run(plan_a, task="mutual delegation")
    assert run.state == RunState.FAILED
    assert "depth" in str(run.final_output)


async def test_mutual_teammates_loop_fails_at_depth_limit():
    # Mutual teammates each run on their own Scheduler, so before the shared
    # ledger every hop reset depth to 0 and the loop never met the guard.
    a = Agent("a", model=ScriptedLlm([ToolCall("b", {"task": "again"})] * 20), instruction="a")
    b = Agent(
        "b",
        model=ScriptedLlm([ToolCall("a", {"task": "again"})] * 20),
        instruction="b",
        teammates=[a],
    )
    a.add_tool(
        ToolSpec(
            name="b",
            description="delegate to b",
            func=b.delegate,
            parameters=_TASK_SCHEMA,
            side_effect="read",
        )
    )
    result = await a.run("start")
    assert "failed" in result.status
    assert "circular delegation" in str(result.output)


class _ExplodingModel:
    async def chat(self, messages, *, tools=None, system=None, on_delta=None):
        raise RuntimeError("model backend exploded")


async def test_teammates_child_failure_propagates():
    # A failed child Run must not come back as a normal tool result: failure
    # propagates up the tree, exactly as with ctx.spawn.
    child = Agent("child", model=_ExplodingModel(), instruction="child")
    parent = Agent(
        "parent",
        model=ScriptedLlm([ToolCall("child", {"task": "do it"}), "done anyway"]),
        instruction="parent",
        teammates=[child],
    )
    result = await parent.run("start")
    assert "failed" in result.status
    assert "model backend exploded" in str(result.output)


async def test_sibling_delegations_do_not_accumulate_depth():
    # Two delegations from one run both sit at depth 1; sequential or same-turn
    # siblings must not add up.
    one = Agent("one", model=ScriptedLlm(["one done"]), instruction="one")
    two = Agent("two", model=ScriptedLlm(["two done"]), instruction="two")
    boss = Agent(
        "boss",
        model=ScriptedLlm(
            [[ToolCall("one", {"task": "x"}), ToolCall("two", {"task": "y"})], "both done"]
        ),
        instruction="boss",
        teammates=[one, two],
    )
    result = await boss.run("go")
    assert "completed" in result.status
    tool_outputs = [m.get("content") for m in result.messages if m.get("role") == "tool"]
    assert "one done" in tool_outputs and "two done" in tool_outputs


async def test_ctxless_delegate_starts_a_fresh_root():
    # handoff/blackboard call delegate() with no ctx: a fresh depth-0 root,
    # untouched by the ledger.
    solo = Agent("solo", model=ScriptedLlm(["solo output"]), instruction="solo")
    assert await solo.delegate("anything") == "solo output"


async def test_run_name_is_blueprint_identity_not_state():
    child = Plan(name="child-expert", channels={"trace": append()})
    child.add(
        Node("work", FnBody(lambda x, ctx: Outcome.ok("child-result", trace=[x])), terminal=True)
    )
    parent = Plan()
    parent.add(Node("delegate", SubPlanBody(child), terminal=True))
    sch = Scheduler()
    started = []
    sch.bus.on("run_started", lambda evt: started.append(evt))

    run = await sch.run(parent, task="给子Agent的任务")

    # Both spawn paths (SubPlanBody here, Agent delegation in the facade)
    # create Runs whose name derives from the plan — no per-run assignment.
    names = [e.data.get("name") for e in started]
    assert "child-expert" in names and "" in names  # the anonymous parent stays nameless
    assert run.name == ""
    # Static identity is never run state: it rides the blueprint, not the
    # snapshot; restore always has the plan at hand.
    snap = run.snapshot()
    assert "name" not in snap
    assert Run.restore(parent, snap).name == ""
