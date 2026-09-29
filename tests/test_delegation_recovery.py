"""Task-level recovery: the delegation fact, suspension lift-up, and resume.

Two layers are tested, each at its own level:
- the kernel's SubPlanBody (a child Plan as a graph node): end-to-end park /
  two-step resume / replay / crash-reattach — the five tests that build a parent
  Plan by hand;
- the runtime's ReAct tool turn: when a model-invoked tool signals a parked
  delegation (DelegationSuspendedError), the tools node lifts it to the whole
  flow, and on resume the re-run tool turn short-circuits via resume_value.
"""

import asyncio
import contextlib

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
)
from src.runtime.llm import ScriptedLlm
from src.runtime.react import build_react_plan, start_react_run
from src.runtime.tools import (
    DelegationSuspendedError,
    HardToolError,
    ToolRegistry,
    ToolSpec,
)


def _parking_child():
    """A specialist that must ask a human before it can answer."""

    async def ask(x, ctx):
        if ctx.resume_value is not None:
            return Outcome.ok(ctx.resume_value)
        return Outcome.park("approval", question="allow the refund?")

    c = Plan()
    c.add(Node("ask", FnBody(ask), terminal=True))
    return c


# ════════════ Kernel: child Plan as a graph node (SubPlanBody) ════════════


async def test_cancelled_parent_resumes_from_snapshot():
    # sync snapshots land at wave ends, so the interrupted wave's nodes are
    # PENDING in the last snapshot: restore + drive simply re-runs that wave
    gate = asyncio.Event()

    async def blocked(x, ctx):
        await gate.wait()
        return Outcome.ok("done")

    plan = Plan()
    plan.add(Node("a", FnBody(lambda x, ctx: Outcome.ok("A"))))
    plan.add(Node("b", FnBody(blocked), terminal=True))
    plan.edge("a", "b")
    plan.entry = ("a",)

    sch = Scheduler()
    run = Run.start(plan, task="go")
    started = asyncio.Event()
    sch.bus.on("node_started", lambda evt: started.set() if evt.data.get("node") == "b" else None)
    task = asyncio.create_task(sch.drive(plan, run))
    await started.wait()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    snap = await sch.store.load(run.run_id)
    assert snap is not None  # wave 1 ("a") was checkpointed before the cancel
    gate.set()
    run2 = Run.restore(plan, snap)
    await sch.drive(plan, run2)
    assert run2.state == RunState.COMPLETED
    assert run2.final_output == "done"
    # falsifiable: "a" must NOT have re-run after the restore (attempts stays 1)
    assert run2.state_of("a").attempts == 1


async def test_child_suspension_parks_parent_with_delegation_fact():
    parent = Plan()
    parent.add(Node("delegate", SubPlanBody(_parking_child()), terminal=True))
    sch = Scheduler()
    facts = []
    sch.bus.on("delegated", lambda evt: facts.append(evt))
    run = await sch.run(parent, task="handle refund")

    assert run.state == RunState.SUSPENDED
    (intr,) = run.interrupts.values()
    assert intr.kind == "delegation"
    assert intr.question == "allow the refund?"
    child_run_id = intr.payload["child_run_id"]
    # the delegation fact is on the parent's stream, attributed to the node
    assert facts and facts[0].data["node"] == "delegate"
    assert facts[0].data["child_run_id"] == child_run_id
    # the parked child has its own snapshot: it can be resumed on its own
    assert await sch.store.load(child_run_id) is not None


async def test_two_step_resume_completes_the_chain_without_respawn():
    child = _parking_child()
    parent = Plan()
    parent.add(Node("delegate", SubPlanBody(child), terminal=True))
    sch = Scheduler()
    births = []
    sch.bus.on("run_started", lambda evt: births.append(evt))
    run = await sch.run(parent, task="handle refund")
    child_run_id = next(iter(run.interrupts.values())).payload["child_run_id"]

    # step 1: resume the child directly; it finishes with an output
    child_run = await sch.resume(child, child_run_id, "yes, approved")
    assert child_run.state == RunState.COMPLETED
    assert child_run.final_output == "yes, approved"
    # step 2: resume the parent with the child's output; the parked node
    # short-circuits — no second child is ever born
    parent_run = await sch.resume(parent, run.run_id, child_run.final_output)
    assert parent_run.state == RunState.COMPLETED
    assert parent_run.final_output == "yes, approved"
    assert len([e for e in births if e.parent_id == run.run_id]) == 1


async def test_replay_skips_the_delegation_fact():
    # DELEGATED is a fact with no state effect: a stream containing it must
    # replay to the same suspended Run, interrupts and all
    from src.kernel import replay

    parent = Plan()
    parent.add(Node("delegate", SubPlanBody(_parking_child()), terminal=True))
    sch = Scheduler()
    run = await sch.run(parent, task="handle refund")
    events = await sch.eventlog.events(run.run_id)
    assert any(e.kind == "delegated" for e in events)  # the fact is in the stream
    replayed = replay(parent, events)
    assert replayed.state == RunState.SUSPENDED
    (live, rebuilt) = next(iter(run.interrupts.values())), next(iter(replayed.interrupts.values()))
    assert rebuilt.kind == live.kind
    assert rebuilt.payload["child_run_id"] == live.payload["child_run_id"]
    assert rebuilt.question == live.question


async def test_delegated_fact_alone_locates_the_child():
    # the crash-reattach recipe: the child's run_id is recoverable from the
    # parent's event stream alone, not only from the live interrupt payload
    child = _parking_child()
    parent = Plan()
    parent.add(Node("delegate", SubPlanBody(child), terminal=True))
    sch = Scheduler()
    run = await sch.run(parent, task="handle refund")
    (fact,) = [e for e in await sch.eventlog.events(run.run_id) if e.kind == "delegated"]
    child_run = await sch.resume(child, fact.data["child_run_id"], "yes, approved")
    parent_run = await sch.resume(parent, run.run_id, child_run.final_output)
    assert parent_run.state == RunState.COMPLETED
    assert parent_run.final_output == "yes, approved"


# ════════════ Runtime: a model-invoked tool in the ReAct turn ════════════


def _scripted_tool(reg: ToolRegistry, name: str, *, behavior: str, delegation: bool = False):
    """Register a tool with scripted behavior for the ReAct turn:
    - "plain": returns a value; "park": a delegated child parked; "boom": hard failure.
    On a re-run after resume, a fed resume_value short-circuits (same law as delegate_to).
    """

    async def fn(ctx=None):
        if ctx is not None and ctx.resume_value is not None:
            return ctx.resume_value
        if behavior == "park":
            raise DelegationSuspendedError(f"{name}-child", f"{name}: allow?")
        if behavior == "boom":
            raise HardToolError(f"{name} backend gone")
        return f"{name} result"

    reg.add(
        ToolSpec(
            name=name,
            description=name,
            func=fn,
            parameters={"type": "object", "properties": {}},
            delegation=delegation,
        )
    )


def _react_drive(reg, script):
    sch = Scheduler(llm=ScriptedLlm(list(script)), tools=reg)
    plan = build_react_plan(reg)
    return sch, plan, start_react_run(plan, "go")


async def test_parked_delegation_lifts_and_resume_short_circuits():
    # one delegation child parks → the whole ReAct flow suspends at the tools node
    reg = ToolRegistry()
    _scripted_tool(reg, "approve", behavior="park", delegation=True)
    sch, plan, run = _react_drive(
        reg, [ToolCall("approve", {"task": "refund #42"}), "refund approved"]
    )
    await sch.drive(plan, run)
    assert run.state == RunState.SUSPENDED
    intr = run.interrupts["tools"]  # the ReAct tool turn is the parked node
    assert intr.kind == "delegation" and intr.question == "approve: allow?"

    # resume the flow: the re-run tool turn gets the value and short-circuits,
    # then the model gives its final answer
    run = await sch.resume(plan, run.run_id, "yes")
    assert run.state == RunState.COMPLETED
    assert run.final_output == "refund approved"


async def test_two_parked_delegations_in_one_turn_are_refused():
    reg = ToolRegistry()
    _scripted_tool(reg, "ask1", behavior="park", delegation=True)
    _scripted_tool(reg, "ask2", behavior="park", delegation=True)
    sch, plan, run = _react_drive(
        reg, [[ToolCall("ask1", {"task": "q1"}), ToolCall("ask2", {"task": "q2"})], "-"]
    )
    await sch.drive(plan, run)
    assert run.state == RunState.FAILED
    assert "call cursor" in str(run.final_output)


async def test_mixed_delegation_turn_is_refused_not_corrupted():
    # a completed sibling delegation plus a parked one: the single resume value
    # cannot route back — refuse rather than silently misroute
    reg = ToolRegistry()
    _scripted_tool(reg, "a", behavior="plain", delegation=True)
    _scripted_tool(reg, "ask", behavior="park", delegation=True)
    sch, plan, run = _react_drive(
        reg, [[ToolCall("a", {"task": "do a"}), ToolCall("ask", {"task": "do ask"})], "-"]
    )
    await sch.drive(plan, run)
    assert run.state == RunState.FAILED
    assert "call cursor" in str(run.final_output)


async def test_failure_wins_over_sibling_suspension():
    # fail-wins, one level down from the wave barrier: a hard failure in the
    # same turn fails the Run even though a sibling delegation parked
    reg = ToolRegistry()
    _scripted_tool(reg, "boom", behavior="boom")  # plain tool that raises HardToolError
    _scripted_tool(reg, "ask", behavior="park", delegation=True)
    sch, plan, run = _react_drive(
        reg, [[ToolCall("boom", {}), ToolCall("ask", {"task": "q"})], "-"]
    )
    await sch.drive(plan, run)
    assert run.state == RunState.FAILED
    assert "backend gone" in str(run.final_output)


async def test_plain_tool_sibling_of_suspension_is_allowed():
    # plain tools re-run honestly at-least-once, so one parked delegation plus
    # plain siblings still suspends and resumes to completion
    reg = ToolRegistry()
    _scripted_tool(reg, "weather", behavior="plain")  # not a delegation
    _scripted_tool(reg, "ask", behavior="park", delegation=True)
    sch, plan, run = _react_drive(
        reg, [[ToolCall("weather", {}), ToolCall("ask", {"task": "q"})], "wrapped up"]
    )
    await sch.drive(plan, run)
    assert run.state == RunState.SUSPENDED

    run = await sch.resume(plan, run.run_id, "yes")
    assert run.state == RunState.COMPLETED
    assert run.final_output == "wrapped up"


async def test_nested_agent_suspension_lifts_to_parent():
    # the grandchild-equivalent parks inside the child's own tool turn; the child
    # Run suspends and the parent facade lifts it — then two-step resume works
    # through Agent.resume.
    from src import Agent as FacadeAgent

    inner_reg = ToolRegistry()
    _scripted_tool(inner_reg, "inner", behavior="park", delegation=True)
    child = FacadeAgent(
        "child",
        model=ScriptedLlm([ToolCall("inner", {}), "child done"]),
        instruction="child",
        registry=inner_reg,
    )
    parent = FacadeAgent(
        "boss",
        model=ScriptedLlm([ToolCall("child", {"task": "do child"}), "all done"]),
        instruction="boss",
        sub_agents=[child],
    )
    r = await parent.run("start")
    assert "suspended" in r.status
    intr = next(iter(r.run.interrupts.values()))
    assert intr.kind == "delegation" and intr.question == "inner: allow?"
    child_run_id = intr.payload["child_run_id"]  # the child Run the boss delegated to

    cr = await child.resume(child_run_id, "yes, approved")
    assert "completed" in cr.status and cr.output == "child done"
    pr = await parent.resume(r.run_id, cr.output)
    assert "completed" in pr.status and pr.output == "all done"
