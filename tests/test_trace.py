"""Trace projection: the event stream folds into a causal tree of Runs."""

from __future__ import annotations

from src.kernel import (
    FnBody,
    Node,
    Plan,
    Scheduler,
    SubPlanBody,
    build_trace,
    render_trace,
)


def _child_plan():
    plan = Plan(name="worker")

    async def work(_input, ctx):
        return "child done"

    plan.add(Node("work", FnBody(work), terminal=True))
    plan.entry = ("work",)
    return plan


def _parent_plan(child):
    plan = Plan(name="boss")
    plan.add(Node("delegate", SubPlanBody(child), terminal=True))
    plan.entry = ("delegate",)
    return plan


async def test_trace_tree_links_parent_and_child():
    sched = Scheduler()
    run = await sched.run(_parent_plan(_child_plan()))
    roots = build_trace(await sched.eventlog.all_events())
    assert len(roots) == 1
    root = roots[0]
    assert root.run_id == run.run_id
    assert root.name == "boss"
    assert len(root.children) == 1
    assert root.children[0].name == "worker"


async def test_render_trace_indents_child_under_parent():
    sched = Scheduler()
    await sched.run(_parent_plan(_child_plan()))
    text = render_trace(await sched.eventlog.all_events())
    assert "● boss" in text
    assert "● worker" in text
    # The child Run's header is indented deeper than its parent's.
    assert text.index("● worker") > text.index("● boss")
    # facts of the parent run are shown
    assert "node_completed" in text


async def test_single_run_trace_has_no_children():
    plan = Plan(name="solo")

    async def step(_input, ctx):
        return 42

    plan.add(Node("step", FnBody(step), terminal=True))
    plan.entry = ("step",)
    sched = Scheduler()
    await sched.run(plan)
    roots = build_trace(await sched.eventlog.all_events())
    assert len(roots) == 1
    assert roots[0].children == []


async def test_durations_survive_a_reload_from_disk(tmp_path):
    # ts must round-trip through FileEventLog: a trace rendered from a durable
    # log (`prodagent trace --dir`) derives durations by subtracting ts — a
    # dropped field would silently read as zero.
    from src.backends.file_store import FileEventLog
    from src.kernel import Event

    log = FileEventLog(str(tmp_path))
    await log.append(Event(1, "r1", "run_started", {"name": "a", "task": "t"}, None, ts=10.0))
    await log.append(Event(2, "r1", "node_started", {"node": "x"}, None, ts=10.5))
    await log.append(Event(3, "r1", "run_completed", {}, None, ts=12.5))

    reloaded = await FileEventLog(str(tmp_path)).all_events()
    assert [e.ts for e in reloaded] == [10.0, 10.5, 12.5]
    (root,) = build_trace(reloaded)
    assert root.duration_ms == 2500
