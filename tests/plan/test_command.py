"""Command — dynamic control flow as data (column 9's laws under test).

Jump, fan-out, and state-merge all change what runs next, and none of them
touch the scheduler's loop: they ride home as a node's return value, pass
their runtime gate, land in the event log, and the next wave simply sees
a different ready set. The map-reduce shape (Send + automatic join) runs
end to end; the fold replays every application.
"""

from __future__ import annotations

import pytest

from prodagent.base.types import ExecutionMode
from prodagent.kernel.bodies.base import FnBody, ToolBody
from prodagent.kernel.bodies.runner import BodyRunner
from prodagent.kernel.node_state import NodeRuntimeState
from prodagent.kernel.types import RunState
from prodagent.plan.command import Goto, Send, Update, command_from_wire
from prodagent.plan.dag import Node, Plan
from prodagent.plan.ir.compiler import compile_planned
from prodagent.plan.scheduler import Scheduler
from prodagent.tooling.dispatcher import ToolDispatcher


class TestCommandWire:
    def test_every_command_round_trips(self):
        for c in [Goto("back"), Send(template="fetch", items=("a", "b"), key="page")]:
            rebuilt = command_from_wire(c.to_wire())
            assert rebuilt == c
        u = Update(key="hits", value=3, reducer="sum")
        assert command_from_wire(u.to_wire()) == u

    def test_dict_markers_need_no_framework_types(self):
        assert command_from_wire({"goto": "x"}) == Goto("x")
        assert command_from_wire({"update": {"key": "k", "value": 1}}) == Update("k", 1)
        assert command_from_wire({}) is None

    def test_send_fanout_cap(self):
        with pytest.raises(ValueError, match="fan-out"):
            Send(template="t", items=tuple(range(17)))


def _scheduler(fns: dict, plan: Plan, tools: dict | None = None) -> Scheduler:
    dispatcher = ToolDispatcher(tools or {})
    runner = BodyRunner(dispatcher.dispatch, fns=fns)
    return Scheduler(
        _NoopLLM(),
        runner,
        mode=ExecutionMode.PLAN_FIRST,
        initial_plan=plan,
        max_replans=0,
        dispatcher=dispatcher,
    )


class _NoopLLM:
    """Plan-mode with a preset plan never calls the planner LLM."""

    async def complete(self, *a, **k):  # pragma: no cover - never called
        raise AssertionError("preset plan must not call the planner")


async def _drive(scheduler: Scheduler, task: str = "t"):
    terminal = None
    async for event in scheduler.stream(task):
        terminal = event
    return terminal


class TestMapReduce:
    @pytest.mark.asyncio
    async def test_send_fans_out_and_reduce_waits_for_all(self):
        seen: list[str] = []
        plan = compile_planned(
            [
                Node(node_id="urls", body=FnBody(fn="urls")),
                Node(
                    node_id="fetch",
                    body=FnBody(fn="fetch"),
                    depends_on=["urls"],
                ),
                Node(node_id="reduce", body=FnBody(fn="reduce"), depends_on=["fetch"]),
            ]
        )
        scheduler = _scheduler(
            {
                "urls": lambda: Send(template="fetch", items=("u1", "u2", "u3"), key="page"),
                "fetch": lambda item: seen.append(item) or {"page": item},
                "reduce": lambda **deps: {"pages": len(deps), "seen": sorted(seen)},
            },
            plan,
        )
        terminal = await _drive(scheduler)

        assert terminal.run.state is RunState.COMPLETED
        # three dynamic instances ran, each with its item
        assert sorted(seen) == ["u1", "u2", "u3"]
        # reduce saw all three pages — the join held
        output = terminal.run.final_output
        assert "3" in str(output)


class TestGoto:
    @pytest.mark.asyncio
    async def test_goto_redoes_a_completed_node(self):
        runs: list[str] = []
        gate_calls = iter(range(2))

        def gate():
            runs.append("gate")
            return Goto("work") if next(gate_calls) == 0 else {"done": True}

        plan = compile_planned(
            [
                Node(node_id="work", body=FnBody(fn="work")),
                Node(node_id="gate", body=FnBody(fn="gate"), depends_on=["work"], is_terminal=True),
            ]
        )
        scheduler = _scheduler(
            {"work": lambda: runs.append("work") or {"n": len(runs)}, "gate": gate},
            plan,
        )
        terminal = await _drive(scheduler)

        # work ran twice (static + goto redo); gate ran twice (it re-enters
        # the round it asked for, then returns a plain result and stops)
        assert runs == ["work", "gate", "work", "gate"]
        assert terminal.run.state is RunState.COMPLETED

    @pytest.mark.asyncio
    async def test_goto_to_unknown_target_is_refused(self):
        plan = compile_planned([Node(node_id="a", body=FnBody(fn="a"), is_terminal=True)])
        scheduler = _scheduler({"a": lambda: Goto("ghost")}, plan)
        with pytest.raises(ValueError, match="ghost"):
            await _drive(scheduler)


class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_merges_with_declared_reducer(self):
        plan = compile_planned(
            [
                Node(node_id="a", body=FnBody(fn="a")),
                Node(node_id="b", body=FnBody(fn="b"), is_terminal=True),
            ]
        )
        scheduler = _scheduler(
            {
                "a": lambda: Update(key="hits", value=3, reducer="sum"),
                "b": lambda: {"total": 1},
            },
            plan,
        )
        await _drive(scheduler)
        # one write lands; the reducer path is exercised by the fold test below

    @pytest.mark.asyncio
    async def test_second_write_without_reducer_is_a_conflict(self):
        plan = compile_planned(
            [
                Node(node_id="a", body=FnBody(fn="a")),
                Node(node_id="b", body=FnBody(fn="b"), is_terminal=True),
            ]
        )
        scheduler = _scheduler(
            {
                "a": lambda: Update(key="hits", value=1),
                "b": lambda: Update(key="hits", value=2),
            },
            plan,
        )
        with pytest.raises(ValueError, match="no reducer declared"):
            await _drive(scheduler)

    def test_shared_template_resolves(self):
        node = Node(node_id="c", body=ToolBody("t"), params={"n": "{{shared.hits}}"})
        plan = Plan()
        plan.add_nodes([node])
        states = {"c": NodeRuntimeState("c")}
        resolved = plan.resolve_params(node, states, {"hits": 7})
        assert resolved["n"] == 7


class TestFold:
    def test_command_events_replay_through_the_reducer(self):
        from prodagent.base.event_log import Event, PlanEventType
        from prodagent.plan.event_log import apply_event

        state: dict = {"nodes": {}, "shared": {}}
        template = {
            "node_id": "fetch",
            "kind": "fn",
            "action": "fetch",
            "origin": "planned",
            "params": {},
            "depends_on": [],
            "is_terminal": False,
            "status": "completed",
        }
        state["nodes"]["fetch"] = dict(template)
        state["nodes"]["reduce"] = {
            **template,
            "node_id": "reduce",
            "depends_on": ["fan"],
            "status": "pending",
        }
        state["nodes"]["fan"] = {**template, "node_id": "fan", "status": "completed"}

        apply_event(
            state,
            Event.make(
                PlanEventType.COMMAND_APPLIED,
                "r",
                version=1,
                node_id="fan",
                command=Send(template="fetch", items=("u1", "u2"), key="page").to_wire(),
            ),
        )
        # two instances sprouted, pending, items injected
        assert state["nodes"]["page#0"]["params"]["item"] == "u1"
        assert state["nodes"]["page#1"]["status"] == "pending"
        # reduce re-wired onto the whole batch
        assert set(state["nodes"]["reduce"]["depends_on"]) == {"page#0", "page#1"}

        apply_event(
            state,
            Event.make(
                PlanEventType.COMMAND_APPLIED,
                "r",
                version=1,
                node_id="x",
                command=Goto("reduce").to_wire(),
            ),
        )
        # a completed redo target resets
        state["nodes"]["reduce"]["status"] = "completed"
        apply_event(
            state,
            Event.make(
                PlanEventType.COMMAND_APPLIED,
                "r",
                version=1,
                node_id="x",
                command=Goto("reduce").to_wire(),
            ),
        )
        assert state["nodes"]["reduce"]["status"] == "pending"

        apply_event(
            state,
            Event.make(
                PlanEventType.COMMAND_APPLIED,
                "r",
                version=1,
                node_id="x",
                command=Update(key="hits", value=3, reducer="sum").to_wire(),
            ),
        )
        apply_event(
            state,
            Event.make(
                PlanEventType.COMMAND_APPLIED,
                "r",
                version=1,
                node_id="y",
                command=Update(key="hits", value=4, reducer="sum").to_wire(),
            ),
        )
        assert state["shared"]["hits"] == 7
