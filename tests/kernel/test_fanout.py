"""Dynamic fan-out and structured concurrency (column 17/18).

Send grows the plan at runtime — the count is data; instances inherit the
template body and land root-ready in the next wave. The join idiom is
``goto("__wait__")``: the collector re-runs each wave until the batch's
results are all in (join counts the registered batch, never the static
in-degree). The readonly wave runs under a TaskGroup: a sibling's failure
is data (brothers keep running), cancellation reaps everyone, and a
wave-scope timeout is a scope cancellation.
"""

from __future__ import annotations

import asyncio

from prodagent.kernel.bodies import FnBody
from prodagent.kernel.channels import merge
from prodagent.kernel.command import WAIT, Goto, Send, Update
from prodagent.kernel.graph import Node, Plan, compile_planned
from prodagent.kernel.scheduler import Scheduler
from prodagent.tooling.dispatcher import ToolDispatcher


def _scheduler(fns: dict, plan: Plan, **kwargs) -> Scheduler:
    return Scheduler(
        initial_plan=plan,
        dispatcher=ToolDispatcher({}),
        fns=fns,
        **kwargs,
    )


async def _drive(scheduler: Scheduler):
    terminal = None
    async for event in scheduler.stream("task"):
        terminal = event
    return terminal


def _fanout_plan() -> Plan:
    return compile_planned(
        [
            Node(node_id="fanout", body=FnBody(fn="fanout")),
            Node(node_id="fetch", body=FnBody(fn="fetch"), is_template=True),
            Node(
                node_id="collect",
                body=FnBody(fn="collect"),
                params={"results": "{{shared.results}}", "task": "{{task}}"},
                depends_on=["fanout"],
                is_terminal=True,
            ),
        ]
    )


async def test_send_fans_out_at_runtime_and_joins_on_the_batch():
    plan = _fanout_plan()
    plan.declare_channels({"results": merge({})})
    fetched: list[str] = []

    def fanout() -> list[Send]:
        urls = ["a", "b", "c"]  # the count is runtime data
        return [Send("fetch", {"url": u}) for u in urls]

    def fetch(url: str) -> Update:
        fetched.append(url)
        return Update("results", {url: f"page-{url}"}, "merge")

    def collect(results: dict, task: str) -> Update | Goto:
        if len(results) < 3:
            return Goto(WAIT)  # not all of the batch is in — look again next wave
        return Update("summary", {"task": task, "pages": len(results)}, "last")

    terminal = await _drive(
        _scheduler({"fanout": fanout, "fetch": fetch, "collect": collect}, plan)
    )
    assert terminal.run.state.value == "completed"
    assert sorted(fetched) == ["a", "b", "c"]
    # N results landed in one merge channel without overwriting each other
    assert terminal.run.shared["results"] == {"a": "page-a", "b": "page-b", "c": "page-c"}
    assert terminal.run.shared["summary"]["pages"] == 3


async def test_send_instances_are_evented_so_the_fold_rebuilds_them():
    from prodagent.backends.memory.event_log import InMemoryEventLog

    log = InMemoryEventLog()
    plan = _fanout_plan()
    plan.declare_channels({"results": merge({})})
    fns = {
        "fanout": lambda: [Send("fetch", {"url": "x"}), Send("fetch", {"url": "y"})],
        "fetch": lambda url: Update("results", {url: url}, "merge"),
        "collect": lambda results, task: {"pages": len(results)},
    }
    terminal = await _drive(_scheduler(fns, plan, event_log=log))
    events = await log.get_events(terminal.run.run_id)
    instantiated = [e for e in events if e.data.get("node", {}).get("origin") == "dynamic"]
    assert len(instantiated) == 2
    # the instances are part of the plan the checkpoint would carry
    assert sum(1 for n in terminal.run.node_states if n.startswith("fetch#")) == 2


async def test_send_to_an_unknown_template_is_a_loud_error():
    plan = compile_planned(
        [
            Node(node_id="entry", body=FnBody(fn="entry"), is_terminal=True),
        ]
    )
    try:
        await _drive(_scheduler({"entry": lambda: Send("nowhere", {})}, plan))
        raise AssertionError("expected a ValueError")
    except ValueError as exc:
        assert "template" in str(exc)


async def test_sibling_failure_is_data_the_wave_finishes():
    """A readonly sibling's crash is a node failure, not a wave panic: the
    brothers run to completion and the failure classifies normally."""
    plan = compile_planned(
        [
            Node(node_id="boom", body=FnBody(fn="boom")),
            Node(node_id="calm", body=FnBody(fn="calm")),
        ]
    )
    ran: list[str] = []

    def boom() -> dict:
        ran.append("boom")
        raise RuntimeError("kaput")

    def calm() -> dict:
        ran.append("calm")
        return {"ok": True}

    terminal = await _drive(_scheduler({"boom": boom, "calm": calm}, plan))
    # both ran; the failure shows up as the run's error path (no replanner
    # wired → the failure settles the run)
    assert sorted(ran) == ["boom", "calm"]
    assert terminal.run.state.value == "failed"


async def test_wave_timeout_cancels_the_straggler():
    from prodagent.kernel.types import RunFailedEvent

    plan = compile_planned(
        [
            Node(node_id="slow", body=FnBody(fn="slow"), is_terminal=True),
        ]
    )

    async def slow() -> dict:  # a straggler past the wave's scope timeout
        await asyncio.sleep(5)
        return {}

    # a plain def returning a coroutine: FnBody awaits awaitable results
    def slow_fn() -> object:
        return slow()

    terminal = await _drive(_scheduler({"slow": slow_fn}, plan, wave_timeout=0.1))
    assert isinstance(terminal, RunFailedEvent)
