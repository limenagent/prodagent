"""Task-level recovery: the delegation fact, suspension lift-up, two-step resume."""

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
from src.runtime.multiagent import build_supervisor
from src.runtime.tools import HardToolError, ToolRegistry


def _parking_child():
    """A specialist that must ask a human before it can answer."""

    async def ask(x, ctx):
        if ctx.resume_value is not None:
            return Outcome.ok(ctx.resume_value)
        return Outcome.park("approval", question="allow the refund?")

    c = Plan()
    c.add(Node("ask", FnBody(ask), terminal=True))
    return c


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


async def test_supervisor_tool_delegation_parks_and_two_step_resumes():
    # the tool-shaped twin: the supervisor's "tool" is a sub-plan that parks
    child = _parking_child()
    registry = ToolRegistry()
    plan = build_supervisor({"approve": (child, "decide refunds")}, registry=registry)
    sch = Scheduler(
        llm=ScriptedLlm([ToolCall("approve", {"task": "check the refund"}), "refund approved"]),
        tools=registry,
    )
    run = await sch.run(plan, task="refund?")
    assert run.state == RunState.SUSPENDED
    intr = run.interrupts["tools"]  # the ReAct tool turn is the parked node
    assert intr.kind == "delegation" and intr.question == "allow the refund?"
    child_run_id = intr.payload["child_run_id"]

    child_run = await sch.resume(child, child_run_id, "yes")
    parent_run = await sch.resume(plan, run.run_id, child_run.final_output)
    assert parent_run.state == RunState.COMPLETED
    assert parent_run.final_output == "refund approved"


def _answering_child(text):
    c = Plan()
    c.add(Node("work", FnBody(lambda x, ctx: Outcome.ok(text)), terminal=True))
    return c


async def test_mixed_delegation_turn_is_refused_not_corrupted():
    # a completed sibling's output is not foldable at park time and the single
    # resume value cannot route back — refuse rather than silently misroute
    registry = ToolRegistry()
    ask = _parking_child()
    plan = build_supervisor(
        {"a": (_answering_child("A-answer"), "answers"), "ask": (ask, "asks")},
        registry=registry,
    )
    sch = Scheduler(
        llm=ScriptedLlm([[ToolCall("a", {"task": "x"}), ToolCall("ask", {"task": "y"})], "-"]),
        tools=registry,
    )
    run = await sch.run(plan, task="go")
    assert run.state == RunState.FAILED
    assert "call cursor" in str(run.final_output)


async def test_failure_wins_over_sibling_suspension():
    # fail-wins, one level down from the wave barrier: a hard failure in the
    # same turn fails the Run even though a sibling delegation parked
    async def boom(task, ctx=None):
        raise HardToolError("backend gone")

    registry = ToolRegistry()
    registry.function(boom, name="boom", description="always fails hard")
    ask = _parking_child()
    plan = build_supervisor({"ask": (ask, "asks")}, registry=registry)
    sch = Scheduler(
        llm=ScriptedLlm([[ToolCall("boom", {"task": "x"}), ToolCall("ask", {"task": "y"})], "-"]),
        tools=registry,
    )
    run = await sch.run(plan, task="go")
    assert run.state == RunState.FAILED
    assert "backend gone" in str(run.final_output)


async def test_plain_tool_sibling_of_suspension_is_allowed():
    # plain tools re-run honestly at-least-once, so one suspended delegation
    # plus plain siblings still parks and two-step-resumes
    async def weather(city, ctx=None):
        return f"sunny in {city}"

    registry = ToolRegistry()
    registry.function(weather, name="weather", description="weather")
    ask = _parking_child()
    plan = build_supervisor({"ask": (ask, "asks")}, registry=registry)
    sch = Scheduler(
        llm=ScriptedLlm(
            [[ToolCall("weather", {"city": "SF"}), ToolCall("ask", {"task": "y"})], "wrapped up"]
        ),
        tools=registry,
    )
    run = await sch.run(plan, task="go")
    assert run.state == RunState.SUSPENDED
    child_run_id = run.interrupts["tools"].payload["child_run_id"]
    child_run = await sch.resume(ask, child_run_id, "yes")
    parent_run = await sch.resume(plan, run.run_id, child_run.final_output)
    assert parent_run.state == RunState.COMPLETED
    assert parent_run.final_output == "wrapped up"


async def test_facade_teammate_suspension_lifts_to_parent():
    # the grandchild parks inside the teammate's own recipe; the teammate's Run
    # suspends, and the facade must lift it (not return None) — two-step resume
    # then works through Agent.resume
    from src import Agent as FacadeAgent

    inner = ToolRegistry()
    ask = _parking_child()
    build_supervisor({"inner": (ask, "asks")}, registry=inner)  # registers "inner"
    child = FacadeAgent(
        "child",
        model=ScriptedLlm([ToolCall("inner", {"task": "check"}), "child done"]),
        instruction="child",
        registry=inner,
    )
    parent = FacadeAgent(
        "boss",
        model=ScriptedLlm([ToolCall("child", {"task": "go"}), "all done"]),
        instruction="boss",
        teammates=[child],
    )
    r = await parent.run("start")
    assert "suspended" in r.status
    intr = next(iter(r.run.interrupts.values()))
    assert intr.kind == "delegation" and intr.question == "allow the refund?"
    child_run_id = intr.payload["child_run_id"]

    cr = await child.resume(child_run_id, "yes, approved")
    assert "completed" in cr.status and cr.output == "child done"
    pr = await parent.resume(r.run_id, cr.output)
    assert "completed" in pr.status and pr.output == "all done"


async def test_two_suspensions_in_one_turn_are_refused():
    registry = ToolRegistry()
    ask1, ask2 = _parking_child(), _parking_child()
    plan = build_supervisor({"ask1": (ask1, "asks"), "ask2": (ask2, "asks too")}, registry=registry)
    sch = Scheduler(
        llm=ScriptedLlm([[ToolCall("ask1", {"task": "x"}), ToolCall("ask2", {"task": "y"})], "-"]),
        tools=registry,
    )
    run = await sch.run(plan, task="go")
    assert run.state == RunState.FAILED
    assert "call cursor" in str(run.final_output)


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
