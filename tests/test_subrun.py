"""Multi-agent: a sub-agent is a child Run recursively driven by some body, using the very same kernel."""

from src.kernel import (
    FnBody,
    Node,
    Outcome,
    Plan,
    RunState,
    Scheduler,
    SubPlanBody,
    append,
)


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
    # forever, so it must fail at max_depth.
    plan_a, plan_b = Plan(), Plan()
    plan_a.add(Node("a", SubPlanBody(plan_b), terminal=True))
    plan_b.add(Node("b", SubPlanBody(plan_a), terminal=True))
    run = await Scheduler(max_depth=3).run(plan_a, task="mutual delegation")
    assert run.state == RunState.FAILED
    assert "depth" in str(run.final_output)
