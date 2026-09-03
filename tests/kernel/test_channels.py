"""Channels — declared state lanes, the barrier fold, and fail-closed conflicts.

Column 7's laws under test: a channel declares init + reducer; same-wave
nodes read the wave-start snapshot; writes fold at the barrier
(order-independent rules fold regardless of completion order); two
same-wave writers on an order-dependent (``last``) channel is
:class:`AmbiguousWrite` — the kernel refuses to guess.
"""

from __future__ import annotations

from typing import Any

import pytest

from prodagent.kernel.bodies import FnBody
from prodagent.kernel.channels import (
    AmbiguousWrite,
    Channel,
    WaveWrites,
    add,
    append,
    apply_channel_inits,
    channel_from_wire,
    last,
)
from prodagent.kernel.command import Update
from prodagent.kernel.graph import Node, Plan, compile_planned
from prodagent.kernel.scheduler import Scheduler
from prodagent.tooling.dispatcher import ToolDispatcher

# ── the declaration vocabulary ────────────────────────────────────────────────


def test_builtin_constructors_carry_their_rule_and_init():
    assert last("start").reducer == "last"
    assert append().init == []
    assert append(["seed"]).init == ["seed"]
    assert add().init == 0
    assert add(5).init == 5


def test_unknown_reducer_name_is_rejected_at_declaration():
    with pytest.raises(ValueError, match="not a declared rule"):
        Channel(init=None, reducer="xor")


def test_last_is_the_one_order_dependent_rule():
    assert not last().is_order_independent
    assert append().is_order_independent
    assert add().is_order_independent


def test_custom_reduce_overrides_the_named_rule():
    ch = Channel(init=0, reducer="add", reduce=lambda old, new: old - new)
    assert ch.resolve()(10, 4) == 6


def test_wire_roundtrip_keeps_init_and_rule_name():
    ch = append(["a"])
    assert channel_from_wire(ch.to_wire()) == ch


def test_wire_drops_a_custom_callable_to_last():
    """A custom reducer is process-local (an Edge-``when`` discipline); the
    wire stays honest rather than lying about a rule it cannot carry."""
    ch = Channel(init=0, reducer="add", reduce=lambda o, n: o + n)
    assert channel_from_wire(ch.to_wire()).reducer == "last"


# ── the barrier machinery ─────────────────────────────────────────────────────


def test_apply_channel_inits_seeds_without_clobbering():
    shared: dict = {"kept": "folded"}
    apply_channel_inits({"notes": append(), "cost": add()}, shared)
    assert shared == {"kept": "folded", "notes": [], "cost": 0}


def test_apply_channel_inits_deep_copies_per_run():
    """A mutable init shared across runs would be the classic trap."""
    channels = {"notes": append(["seed"])}
    a: dict = {}
    b: dict = {}
    apply_channel_inits(channels, a)
    apply_channel_inits(channels, b)
    a["notes"].append("run-a")
    assert b["notes"] == ["seed"]


def test_wave_writes_buffer_then_drain_in_arrival_order():
    w = WaveWrites({"notes": append()})
    w.buffer("notes", "a", "n1")
    w.buffer("notes", "b", "n2")
    assert w
    assert w.drain() == [("notes", "a", "n1"), ("notes", "b", "n2")]
    assert not w


def test_wave_writes_fail_closed_on_same_wave_last_writers():
    w = WaveWrites({"answer": last()})
    w.buffer("answer", 1, "n1")
    w.buffer("answer", 2, "n2")
    with pytest.raises(AmbiguousWrite, match="answer") as exc:
        w.check_ambiguous()
    assert sorted(exc.value.writers) == ["n1", "n2"]


def test_wave_writes_allow_many_writers_on_order_independent_rules():
    w = WaveWrites({"notes": append()})
    w.buffer("notes", "a", "n1")
    w.buffer("notes", "b", "n2")
    w.check_ambiguous()  # does not raise


# ── plan-level declaration and the wire ───────────────────────────────────────


def _one_wave_plan() -> Plan:
    return compile_planned(
        [
            Node(node_id="only", body=FnBody(fn="only"), is_terminal=True),
        ]
    )


def test_plan_declares_channels_and_survives_the_wire():
    plan = _one_wave_plan()
    plan.declare_channels({"notes": append(), "cost": add()})
    state = plan.to_state({})
    assert set(state["channels"]) == {"notes", "cost"}
    rebuilt, _states = Plan.from_state(state, plan_id=plan.plan_id)
    assert rebuilt.channels == plan.channels


def test_plan_without_channels_wires_without_the_key():
    """Legacy checkpoints predate channels; the wire omits the key entirely."""
    state = _one_wave_plan().to_state({})
    assert "channels" not in state


def test_derive_carries_the_declaration():
    plan = _one_wave_plan()
    plan.declare_channels({"notes": append()})
    derived = plan.derive(plan_id="run-1", task_input="t")
    assert derived.channels == {"notes": append()}


# ── end to end through the one engine ─────────────────────────────────────────


def _scheduler(fns: dict, plan: Plan) -> Scheduler:
    return Scheduler(initial_plan=plan, dispatcher=ToolDispatcher({}), fns=fns)


async def _drive(scheduler: Scheduler):
    terminal = None
    async for event in scheduler.stream("task"):
        terminal = event
    return terminal


def _fan_plan() -> Plan:
    """Two independent writers (one wave) then a reader (next wave)."""
    return compile_planned(
        [
            Node(node_id="w1", body=FnBody(fn="w1")),
            Node(node_id="w2", body=FnBody(fn="w2")),
            Node(
                node_id="sink",
                body=FnBody(fn="sink"),
                params={"seen": "{{shared.notes}}"},
                depends_on=["w1", "w2"],
                is_terminal=True,
            ),
        ]
    )


class _Write:
    """A fn-step author's channel write: return an Update, and on a declared
    channel the rule comes from the blueprint (stated once, not per writer)."""

    def __init__(self, key: str, value: Any, reducer: str | None = None) -> None:
        self.key, self.value, self.reducer = key, value, reducer

    def __call__(self) -> Update:
        return Update(self.key, self.value, self.reducer)


async def test_same_wave_writers_on_an_append_channel_all_survive():
    plan = _fan_plan()
    plan.declare_channels({"notes": append(["seed"])})
    calls: list = []

    def sink(seen) -> dict:
        calls.append(seen)
        return {"ok": True}

    terminal = await _drive(
        _scheduler(
            {"w1": _Write("notes", "a"), "w2": _Write("notes", "b"), "sink": sink},
            plan,
        )
    )
    assert terminal.run.state.value == "completed"
    # the barrier folded both writes onto the init, order-free — neither
    # write covered the other, and the result does not depend on which
    # same-wave node finished first
    assert sorted(terminal.run.shared["notes"]) == ["a", "b", "seed"]
    # and the next wave's reader saw the folded value through {{shared.…}}
    assert sorted(calls[0]) == ["a", "b", "seed"]


async def test_same_wave_reader_sees_the_wave_start_snapshot():
    """w2 runs in w1's wave and reads the channel: it must see the wave-start
    value (the init), never w1's not-yet-folded write."""

    plan = compile_planned(
        [
            Node(node_id="w1", body=FnBody(fn="w1")),
            Node(
                node_id="w2",
                body=FnBody(fn="w2"),
                params={"seen": "{{shared.notes}}"},
            ),
            Node(
                node_id="sink",
                body=FnBody(fn="sink"),
                depends_on=["w1", "w2"],
                is_terminal=True,
            ),
        ]
    )
    plan.declare_channels({"notes": append(["seed"])})

    def w2(seen) -> dict:
        return {"saw": seen}

    def sink() -> dict:
        return {}

    terminal = await _drive(_scheduler({"w1": _Write("notes", "a"), "w2": w2, "sink": sink}, plan))
    assert terminal.run.state.value == "completed"
    # whichever order the wave ran in, w2 saw the snapshot, not the buffer
    assert terminal.run.node_states["w2"].output_ref["saw"] == ["seed"]
    # and the fold still landed for the waves after
    assert sorted(terminal.run.shared["notes"]) == ["a", "seed"]


async def test_two_same_wave_writers_on_a_last_channel_fail_closed():
    plan = _fan_plan()
    plan.declare_channels({"answer": last(0)})
    scheduler = _scheduler(
        {"w1": _Write("answer", 1), "w2": _Write("answer", 2), "sink": lambda seen: {}},
        plan,
    )
    with pytest.raises(AmbiguousWrite, match="answer"):
        await _drive(scheduler)


async def test_single_writer_on_a_last_channel_lands():
    plan = compile_planned(
        [
            Node(node_id="w1", body=FnBody(fn="w1")),
            Node(
                node_id="sink",
                body=FnBody(fn="sink"),
                depends_on=["w1"],
                is_terminal=True,
            ),
        ]
    )
    plan.declare_channels({"answer": last("init")})
    terminal = await _drive(_scheduler({"w1": _Write("answer", "final"), "sink": lambda: {}}, plan))
    assert terminal.run.state.value == "completed"
    assert terminal.run.shared["answer"] == "final"


async def test_channel_writes_are_events_the_log_can_replay():
    """Channel state's truth is the log: the barrier's folds land as
    COMMAND_APPLIED updates carrying the channel's rule name."""
    from prodagent.backends.memory.event_log import InMemoryEventLog

    log = InMemoryEventLog()
    plan = _fan_plan()
    plan.declare_channels({"notes": append()})
    scheduler = Scheduler(
        initial_plan=plan,
        dispatcher=ToolDispatcher({}),
        fns={"w1": _Write("notes", "a"), "w2": _Write("notes", "b"), "sink": lambda seen: {}},
        event_log=log,
    )
    terminal = await _drive(scheduler)
    events = await log.get_events(terminal.run.run_id)
    updates = [
        e.data["command"]["update"] for e in events if (e.data.get("command") or {}).get("update")
    ]
    assert sorted(u["key"] for u in updates) == ["notes", "notes"]
    assert all(u["reducer"] == "append" for u in updates)
