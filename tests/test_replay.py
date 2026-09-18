"""Replay: the event stream alone, folded by a pure projector (no scheduler,
no body, no LLM, no tool), rebuilds a Run equivalent to the live one."""

from src.kernel import (
    FnBody,
    Goto,
    Node,
    NodeStatus,
    Outcome,
    Plan,
    RunState,
    Scheduler,
    Send,
    last,
    replay,
)


async def test_replay_rebuilds_dynamic_fanout():
    """plan-first shape: planner Sends N template workers and re-joins synth;
    instances, their outputs, shared state and the ending all come back."""

    async def planner(_input, ctx):
        steps = [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]
        sends = [Send("worker", s, key=s["id"]) for s in steps]
        return Outcome(state_delta={"steps": steps}, control=[*sends, Goto.rejoin("synth")])

    async def worker(step, ctx):
        return Outcome.ok(f"done-{step['id']}")

    async def synth(inputs, ctx):
        return Outcome.ok(inputs)

    p = Plan(channels={"steps": last([])})
    p.add(
        Node("planner", FnBody(planner)),
        Node("worker", FnBody(worker), template=True),
        Node("synth", FnBody(synth), terminal=True),
    )
    p.edge("worker", "synth")
    p.entry = ("planner",)

    sch = Scheduler()
    run = await sch.run(p)
    assert run.state == RunState.COMPLETED

    r2 = replay(p, await sch.eventlog.events(run.run_id))
    assert r2.state == RunState.COMPLETED
    assert r2.shared["steps"] == run.shared["steps"]
    assert sorted(r2.instances["worker"]) == sorted(run.instances["worker"])
    for key in run.instances["worker"]:
        live, rebuilt = run.state_of(key), r2.state_of(key)
        assert rebuilt.status == NodeStatus.COMPLETED
        assert rebuilt.output == live.output
    assert r2.state_of("synth").status == NodeStatus.COMPLETED
    assert sorted(r2.final_output) == sorted(run.final_output)
    assert r2.deliveries == {}


async def test_replay_rebuilds_goto_back_edge():
    """An immediate Goto re-arms the same node each wave; replay walks the loop
    and lands on the terminal node with the same folded state."""

    async def loop(_input, ctx):
        n = ctx.shared.get("n", 0) + 1
        target = "loop" if n < 3 else "done"
        return Outcome.goto(target, n=n)

    p = Plan(channels={"n": last(0)})
    p.add(
        Node("loop", FnBody(loop)),
        Node("done", FnBody(lambda i, ctx: Outcome.ok(i)), terminal=True),
    )
    p.edge("loop", "done", when=lambda s: s["n"] >= 3)
    p.entry = ("loop",)

    sch = Scheduler()
    run = await sch.run(p)
    r2 = replay(p, await sch.eventlog.events(run.run_id))

    assert r2.state == RunState.COMPLETED
    assert r2.shared["n"] == 3
    assert r2.state_of("loop").status == NodeStatus.COMPLETED
    assert r2.final_output == run.final_output


async def test_replay_marks_untaken_branch_skipped():
    """A conditional edge that evaluates false structurally skips a branch;
    node_skipped events let replay mark it the same way."""

    p = Plan(channels={"route": last(None)})
    p.add(
        Node("decide", FnBody(lambda i, ctx: Outcome.ok(None, route="a"))),
        Node("a", FnBody(lambda i, ctx: Outcome.ok("A")), terminal=True),
        Node("b", FnBody(lambda i, ctx: Outcome.ok("B"))),
    )
    p.edge("decide", "a", when=lambda s: s.get("route") == "a")
    p.edge("decide", "b", when=lambda s: s.get("route") == "b")

    sch = Scheduler()
    run = await sch.run(p)
    assert run.state == RunState.COMPLETED

    r2 = replay(p, await sch.eventlog.events(run.run_id))
    assert r2.state_of("a").status == NodeStatus.COMPLETED
    assert r2.state_of("b").status == NodeStatus.SKIPPED
    assert r2.final_output == "A"


async def test_replay_rebuilds_a_suspension():
    """A run parked on a human: replay stops SUSPENDED with the same question
    and payload, without anyone actually answering."""

    async def ask(_input, ctx):
        return Outcome.park("input", {"order": "o1"}, question="need the order id")

    async def after(_input, ctx):
        return Outcome.ok("finished")

    p = Plan()
    p.add(Node("ask", FnBody(ask)), Node("after", FnBody(after), terminal=True))
    p.edge("ask", "after")

    sch = Scheduler()
    run = await sch.run(p)
    assert run.state == RunState.SUSPENDED

    r2 = replay(p, await sch.eventlog.events(run.run_id))
    assert r2.state == RunState.SUSPENDED
    parked = r2.interrupts["ask"]
    assert parked.question == "need the order id"
    assert parked.payload == {"order": "o1"}


async def test_replay_requires_a_run_started_event():
    p = Plan().add(Node("a", FnBody(lambda i, ctx: "x"), terminal=True))
    try:
        replay(p, [])
    except ValueError:
        return
    raise AssertionError("replaying an empty stream must fail loudly")


async def test_replay_recovers_the_opening_user_message():
    # The ReAct seed (opening user message) is folded through a state_delta event,
    # not written straight to shared state, so a pure replay rebuilds the dialogue
    # from its very first line.
    from src.kernel import ToolCall
    from src.kernel.eventlog import STATE_DELTA
    from src.runtime.llm import ScriptedLlm
    from src.runtime.react import build_react_plan, start_react_run
    from src.runtime.tools import ToolRegistry

    reg = ToolRegistry()

    async def search(query, ctx):
        return f"结果({query})"

    reg.function(search, description="搜索")
    plan = build_react_plan(reg)
    sch = Scheduler(llm=ScriptedLlm([ToolCall("search", {"query": "x"}), "最终答案"]), tools=reg)
    run = start_react_run(plan, "查一下")
    await sch.drive(plan, run)
    assert run.state == RunState.COMPLETED

    events = await sch.eventlog.events(run.run_id)
    seed_deltas = [
        e.data["delta"]
        for e in events
        if e.kind == STATE_DELTA and "messages" in e.data.get("delta", {})
    ]
    assert seed_deltas, "the opening message must be an event, not a direct state write"
    assert seed_deltas[0]["messages"][0] == {"role": "user", "content": "查一下"}

    r2 = replay(plan, events)
    assert r2.shared["messages"][0] == {"role": "user", "content": "查一下"}
    assert r2.shared["messages"][-1]["role"] == "assistant"
