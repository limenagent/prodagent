"""The two ledger laws, as tests.

1. replay ≡ live: at every stop (parked / failed / completed), rebuilding the
   Run from its recorded facts yields exactly the live Run's ledger state.
   langgraph cannot write this test (recovery there is version arithmetic, not
   a fold); ADK cannot either (its state is a materialized cache beside the
   log). Ours passes by construction — this test guards the construction
   against future drift.

2. model-visible means logged: every message window the model was shown is
   reconstructable by folding the STATE_DELTA facts. What the model sees is a
   projection of the log, never a private side channel (dsh enforces the same
   invariant at dispatch time; we pin it here against ScriptedLlm's record).
"""

import pytest

from src import Agent
from src.backends.file_store import FileEventLog
from src.kernel import (
    FnBody,
    Node,
    Outcome,
    Plan,
    RunState,
    Scheduler,
    ToolCall,
    append,
    replay,
)
from src.kernel.eventlog import STATE_DELTA
from src.runtime.llm import ScriptedLlm


# ---- scenarios: each factory drives to a stop, fresh per test ----
async def _approval(stop_at_park: bool):
    async def ask(_x, ctx):
        if ctx.resume_value is None:
            return Outcome.park("approval", question="approve?")
        return Outcome.ok(ctx.resume_value)

    plan = Plan().add(Node("ask", FnBody(ask), terminal=True))
    sch = Scheduler()
    run = await sch.run(plan, task="t")
    if not stop_at_park and run.state == RunState.SUSPENDED:
        run = await sch.resume(plan, run.run_id, {"approved": True})
    return plan, sch.eventlog, run


async def _fanout_with_join():
    async def fan(_x, _ctx):
        return [Outcome.send("work", "a"), Outcome.send("work", "b")]

    plan = Plan(channels={"results": append()})
    plan.add(Node("fan", FnBody(fan)))
    plan.add(
        Node(
            "work",
            FnBody(lambda x, ctx: Outcome.ok(str(x), results=[str(x)])),
            template=True,
        )
    )
    plan.add(Node("join", FnBody(lambda x, ctx: Outcome.ok(sorted(x))), terminal=True))
    plan.edge("fan", "join")
    plan.entry = ("fan",)
    sch = Scheduler()
    return plan, sch.eventlog, await sch.run(plan, task="t")


async def _fail_wins_wave():
    async def boom(_x, _ctx):
        raise RuntimeError("boom")

    plan = Plan().add(Node("boom", FnBody(boom), terminal=True))
    sch = Scheduler()
    return plan, sch.eventlog, await sch.run(plan, task="t")


async def _react_two_rounds():
    agent = Agent(
        name="law",
        model=ScriptedLlm([ToolCall("ping", {}), ToolCall("ping", {}), "done after two rounds"]),
        instruction="i",
        tools=[lambda ctx=None: "pong"],
    )
    result = await agent.run("go")
    return agent.plan, agent.host().eventlog, result.run


async def _delegation_child():
    from src.kernel import SubPlanBody

    child = Plan(name="child")
    child.add(Node("work", FnBody(lambda x, ctx: Outcome.ok(f"child:{x}")), terminal=True))
    plan = Plan().add(Node("delegate", SubPlanBody(child), terminal=True))
    sch = Scheduler()
    return plan, sch.eventlog, await sch.run(plan, task="hand it down")


SCENARIOS = {
    "approval-parked": lambda: _approval(True),
    "approval-resumed": lambda: _approval(False),
    "fanout-join": _fanout_with_join,
    "fail-wins": _fail_wins_wave,
    "react-loop": _react_two_rounds,
    "delegation": _delegation_child,
}


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_replay_equals_live_at_every_stop(name):
    plan, eventlog, run = await SCENARIOS[name]()
    again = replay(plan, await eventlog.events(run.run_id))
    assert again.state == run.state
    assert again.final_output == run.final_output
    assert again.shared == run.shared
    assert {k: (s.status, s.output, s.attempts) for k, s in again.node_states.items()} == {
        k: (s.status, s.output, s.attempts) for k, s in run.node_states.items()
    }
    assert again.instances == run.instances
    assert again.deliveries == run.deliveries
    assert again.activated == run.activated
    assert again.resume_values == run.resume_values
    assert again.parent_id == run.parent_id and again.depth == run.depth


async def test_replay_equals_live_through_a_json_file_log(tmp_path):
    """The same law through the durable .jsonl: a second process shape (a fresh
    FileEventLog on the same directory) must rebuild the identical Run — a
    non-native payload would either crash the boundary guard or drift."""

    async def ask(_x, ctx):
        if ctx.resume_value is None:
            return Outcome.park("approval", question="approve?")
        return Outcome.ok(ctx.resume_value)

    plan = Plan().add(Node("ask", FnBody(ask), terminal=True))
    sch = Scheduler(eventlog=FileEventLog(str(tmp_path)))
    run = await sch.run(plan, task="t")
    run = await sch.resume(plan, run.run_id, {"approved": True})
    assert run.state == RunState.COMPLETED

    again = replay(plan, await FileEventLog(str(tmp_path)).events(run.run_id))
    assert again.state == run.state
    assert again.final_output == run.final_output
    assert again.shared == run.shared


async def test_file_log_refuses_non_native_payloads(tmp_path):
    """The boundary guard: making a fact with a live object inside fails at
    the writer, loudly, with the path to the offending field."""
    from src.kernel.eventlog import Event

    log = FileEventLog(str(tmp_path))
    with pytest.raises(TypeError, match=r"data\.delta\.messages\[0\]"):
        await log.append(Event(1, "r", STATE_DELTA, {"delta": {"messages": [ToolCall("x", {})]}}))


async def test_model_visible_means_logged():
    """Every window the model saw equals a prefix of the folded facts, landing
    exactly on a fact boundary: the request is a projection of the log, never
    a parallel truth."""
    from src.kernel import fold_events

    llm = ScriptedLlm([ToolCall("ping", {}), ToolCall("ping", {}), "done after two rounds"])
    agent = Agent(name="law", model=llm, instruction="i", tools=[lambda ctx=None: "pong"])
    result = await agent.run("go")
    sch = agent.host()
    events = await sch.eventlog.events(result.run_id)
    deltas = [e for e in events if e.kind == STATE_DELTA]

    folded = fold_events(deltas, agent.plan.channels, agent.plan.initial_shared())["messages"]
    # cumulative message counts after each fact = the legal window sizes
    boundaries = set()
    running = fold_events([], agent.plan.channels, agent.plan.initial_shared())["messages"]
    boundaries.add(len(running))
    for d in deltas:
        running = running + list(d.data["delta"].get("messages", []))
        boundaries.add(len(running))

    assert llm.messages_seen, "the scenario must have recorded windows"
    for window in llm.messages_seen:
        assert folded[: len(window)] == window
        assert len(window) in boundaries
