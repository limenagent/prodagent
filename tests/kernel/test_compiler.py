"""compiler — the code→graph front-end (column 6).

The laws under test: a written order of steps becomes sequence edges; an
``if`` becomes a conditional edge (the branch is chosen at runtime by the
predicate, never scrapped early); a ``while`` becomes a back edge (the loop
turns while the predicate holds, then exits); a parallel block fans out and
all-joins; and the compiled graph drives through the one Scheduler exactly
like any hand-written Plan.
"""

from __future__ import annotations

from prodagent.kernel.channels import add, last
from prodagent.kernel.command import Update
from prodagent.kernel.compiler import CompileError
from prodagent.kernel.compiler import compile as wcompile
from prodagent.kernel.scheduler import Scheduler
from prodagent.tooling.dispatcher import ToolDispatcher

# ── the compile-time translation ─────────────────────────────────────────────


def _fetch():
    return {}


def _analyze():
    return {}


def _report():
    return {}


async def _if_body(ctx, s):
    await ctx.call(_fetch)
    if s.need_deep:
        await ctx.call(_analyze)
    await ctx.call(_report)


async def _while_body(ctx, s):
    await ctx.call(_fetch)
    while s.more:
        await ctx.call(_analyze)
    await ctx.call(_report)


async def _parallel_body(ctx, s):
    await ctx.call(_fetch)
    async with ctx.parallel() as p:
        p.call(_analyze)
        p.call(_report)
    await ctx.call(_fetch)


def test_sequence_becomes_edges_and_if_a_conditional_edge():
    c = wcompile(_if_body)
    # the merge gate is join-any (exactly one branch runs)
    gate = next(n for n in c.plan.nodes.values() if n.join == "any")
    assert gate is not None
    # the written order becomes edges: fetch fans into the branch and the
    # fall-through, both merge at the gate, then report follows
    nodes = set(c.plan.nodes)
    fetch = next(n for n in nodes if n.startswith("_fetch"))
    analyze = next(n for n in nodes if n.startswith("_analyze"))
    report = next(n for n in nodes if n.startswith("_report"))
    edges = {(e.source, e.target) for e in c.plan.edges}
    assert {
        (fetch, analyze),
        (fetch, gate.node_id),
        (analyze, gate.node_id),
        (gate.node_id, report),
    } <= edges
    # the branch edge carries a predicate; the fall-through does too
    when_edges = [e for e in c.plan.edges if e.when is not None]
    assert len(when_edges) == 2
    # every step fn is registered
    assert {"_fetch", "_analyze", "_report"} <= set(c.fns)


def test_while_becomes_a_back_edge():
    c = wcompile(_while_body)
    back = c.plan.back_edges()
    assert len(back) == 1  # the loop's body → tail edge, and only it
    (e,) = back
    assert e.back is True


def test_parallel_fans_out_and_all_joins():
    c = wcompile(_parallel_body)
    joins = {n.node_id: n.join for n in c.plan.nodes.values()}
    all_joins = [nid for nid, j in joins.items() if j == "all" and nid.startswith("gate")]
    assert all_joins, "the parallel block needs an all-join gate"


def test_unsupported_statement_is_a_loud_compile_error():
    async def bad(ctx, s):
        for x in range(3):  # noqa: B007 — the unsupported shape
            await ctx.call(_fetch)

    try:
        wcompile(bad)
        raise AssertionError("expected CompileError")
    except CompileError as exc:
        assert "unsupported statement" in str(exc)


# ── end-to-end: the compiled graph drives like any Plan ──────────────────────
# Step functions must be module-level (the compiler resolves them from the
# body's globals — column 6's @step form), so they live here with a module
# mutable for observing order.


_E2E_CALLS: list[str] = []


def _e2e_fetch():
    _E2E_CALLS.append("fetch")
    return Update("need_deep", True, "last")


def _e2e_analyze():
    _E2E_CALLS.append("analyze")
    return {}


def _e2e_report():
    _E2E_CALLS.append("report")
    return {}


async def _e2e_if_body(ctx, s):
    await ctx.call(_e2e_fetch)
    if s.need_deep:
        await ctx.call(_e2e_analyze)
    await ctx.call(_e2e_report)


async def _drive(scheduler: Scheduler):
    terminal = None
    async for ev in scheduler.stream("task"):
        terminal = ev
    return terminal


async def test_if_branch_is_chosen_at_runtime_not_scrapped_early():
    _E2E_CALLS.clear()
    c = wcompile(_e2e_if_body)
    c.plan.declare_channels({"need_deep": last(False)})
    terminal = await _drive(
        Scheduler(initial_plan=c.plan, fns=c.fns, dispatcher=ToolDispatcher({}))
    )
    assert _E2E_CALLS == ["fetch", "analyze", "report"], _E2E_CALLS
    assert terminal.run.state.value == "completed"


_W_CALLS: list[str] = []


def _w_fetch():
    _W_CALLS.append("fetch")
    return Update("count", 0, "last")


def _w_step():
    _W_CALLS.append("step")
    return Update("count", 1, "add")


def _w_report():
    _W_CALLS.append("report")
    return {}


async def _w_body(ctx, s):
    await ctx.call(_w_fetch)
    while s.count < 3:
        await ctx.call(_w_step)
    await ctx.call(_w_report)


async def test_while_loop_turns_then_exits():
    _W_CALLS.clear()
    c = wcompile(_w_body)
    c.plan.declare_channels({"count": add(0)})
    terminal = await _drive(
        Scheduler(initial_plan=c.plan, fns=c.fns, dispatcher=ToolDispatcher({}))
    )
    assert _W_CALLS == ["fetch", "step", "step", "step", "report"], _W_CALLS
    assert terminal.run.shared["count"] == 3
