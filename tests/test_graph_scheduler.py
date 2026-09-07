"""Graph and scheduler: sequential, diamond concurrency, conditional branches, back-edge loops, dynamic fan-out/fan-in, stagnation and spinning."""

import pytest

from src.kernel import (
    FnBody,
    Node,
    Outcome,
    Plan,
    Run,
    RunState,
    Scheduler,
    Send,
    add,
    append,
    last,
)


async def run_plan(plan, task="t"):
    return await Scheduler().run(plan, task=task)


# —— sequential execution ——
async def test_linear():
    p = Plan(channels={"log": append()})
    p.add(
        Node("a", FnBody(lambda x, ctx: Outcome.ok("A", log=["a"]))),
        Node("b", FnBody(lambda x, ctx: Outcome.ok(x + "B", log=["b"]))),
        Node("c", FnBody(lambda x, ctx: Outcome.ok(x + "C")), terminal=True),
    )
    p.edge("a", "b")
    p.edge("b", "c")
    run = await run_plan(p)
    assert run.state == RunState.COMPLETED
    assert run.final_output == "ABC"
    assert run.shared["log"] == ["a", "b"]
    assert run.metrics["waves"] == 3


# —— diamond concurrency: append loses nothing, add sums ——
async def test_diamond_parallel_reducers():
    p = Plan(channels={"items": append(), "n": add(0)})
    p.add(
        Node("a", FnBody(lambda x, ctx: Outcome.ok(None, items=["a"], n=1))),
        Node("b", FnBody(lambda x, ctx: Outcome.ok("B", items=["b"], n=1))),
        Node("c", FnBody(lambda x, ctx: Outcome.ok("C", items=["c"], n=1))),
        Node("d", FnBody(lambda x, ctx: Outcome.ok(sorted(ctx.shared["items"]))), terminal=True),
    )
    p.edge("a", "b")
    p.edge("a", "c")
    p.edge("b", "d")
    p.edge("c", "d")
    run = await run_plan(p)
    assert run.state == RunState.COMPLETED
    assert run.final_output == ["a", "b", "c"]
    assert run.shared["n"] == 3


# —— conditional branch: takes the left path, the right path is structurally skipped ——
async def test_conditional_branch_skips_dead_path():
    p = Plan(channels={"go_left": last(False)})
    p.add(
        Node("start", FnBody(lambda x, ctx: Outcome.ok(None, go_left=True))),
        Node("left", FnBody(lambda x, ctx: Outcome.ok("L")), terminal=True),
        Node("right", FnBody(lambda x, ctx: Outcome.ok("R")), terminal=True),
    )
    p.edge("start", "left", when=lambda s: s["go_left"])
    p.edge("start", "right", when=lambda s: not s["go_left"])
    run = await run_plan(p)
    assert run.final_output == "L"
    assert run.state_of("right").status.value == "skipped"


# —— back-edge loop (Goto re-arms its target) ——
async def test_goto_loop():
    p = Plan(channels={"c": add(0)})

    def tick(x, ctx):
        return Outcome.goto("tick", c=1) if ctx.shared["c"] < 2 else Outcome.ok("stop", c=1)

    p.add(Node("tick", FnBody(tick), terminal=True))
    run = await run_plan(p)
    assert run.final_output == "stop"
    assert run.shared["c"] == 3


# —— dynamic fan-out + fan-in ——
async def test_dynamic_fanout_fanin():
    p = Plan(channels={"outs": append()})
    p.add(
        Node(
            "fan",
            FnBody(
                lambda x, ctx: Outcome.fan_out(
                    Send("worker", 1, key="w1"), Send("worker", 2, key="w2")
                )
            ),
        ),
        Node("worker", FnBody(lambda x, ctx: Outcome.ok(None, outs=[x * 10])), template=True),
        Node("join", FnBody(lambda x, ctx: Outcome.ok(sorted(ctx.shared["outs"]))), terminal=True),
    )
    p.edge("worker", "join")
    run = await run_plan(p)
    assert run.final_output == [10, 20]
    assert sorted(run.instances["worker"]) == ["worker#w1", "worker#w2"]


# —— an entry-less cycle: both wait on each other, neither can start; must fail loudly, not silently succeed ——
async def test_stagnation_fails():
    p = Plan()
    p.add(
        Node("a", FnBody(lambda x, ctx: Outcome.ok("A"))),
        Node("b", FnBody(lambda x, ctx: Outcome.ok("B"))),
    )
    p.edge("a", "b")
    p.edge("b", "a")  # a waits for b, b waits for a, and there is no entry node
    run = await run_plan(p)
    assert run.state == RunState.FAILED
    assert "stalled" in (run.final_output or "")


# —— a back-edge that never makes progress hits the max_waves spin guard ——
async def test_max_waves_guards_spin():
    p = Plan(channels={"c": add(0)})
    p.add(Node("spin", FnBody(lambda x, ctx: Outcome.goto("spin", c=1)), terminal=True))
    sch = Scheduler(max_waves=5)
    run = await sch.run(p)
    assert run.state == RunState.FAILED
    assert "max waves" in run.final_output


# —— state machine: an illegal transition is rejected outright ——
async def test_illegal_transition_rejected():
    p = Plan()
    p.add(Node("a", FnBody(lambda x, ctx: Outcome.ok("A")), terminal=True))
    run = await run_plan(p)
    with pytest.raises(RuntimeError):
        run.complete("again")  # already completed, cannot complete again


# —— join policy is validated at build time, not discovered mid-run ——
async def test_invalid_join_rejected_at_validate():
    p = Plan()
    p.add(Node("a", FnBody(lambda x, ctx: Outcome.ok("A")), join="most"))
    with pytest.raises(ValueError, match="invalid join"):
        p.validate()


# —— join="any": one live sibling completing is enough, the skipped one is not waited on ——
async def test_join_any_fires_when_a_sibling_was_structurally_skipped():
    p = Plan(channels={"go_a": last(True)})
    p.add(
        Node("start", FnBody(lambda x, ctx: Outcome.ok(None, go_a=True))),
        Node("a", FnBody(lambda x, ctx: Outcome.ok("A"))),
        Node("b", FnBody(lambda x, ctx: Outcome.ok("B"))),
        Node("c", FnBody(lambda x, ctx: Outcome.ok("C")), join="any", terminal=True),
    )
    p.edge("start", "a", when=lambda s: s["go_a"])
    p.edge("start", "b", when=lambda s: not s["go_a"])
    p.edge("a", "c")
    p.edge("b", "c")
    run = await run_plan(p)
    assert run.state == RunState.COMPLETED
    assert run.final_output == "C"
    assert run.state_of("b").status.value == "skipped"


# —— join as a callable: a custom (done, total) -> bool quorum, not just all/any ——
async def test_join_callable_quorum_escape_hatch():
    def quorum2(done, total):
        return done >= 2

    p = Plan()
    p.add(
        Node("start", FnBody(lambda x, ctx: Outcome.ok(None))),
        Node("a", FnBody(lambda x, ctx: Outcome.ok("A"))),
        Node("b", FnBody(lambda x, ctx: Outcome.ok("B"))),
        Node("c", FnBody(lambda x, ctx: Outcome.ok("C"))),
        Node(
            "done", FnBody(lambda x, ctx: Outcome.ok("quorum-reached")), join=quorum2, terminal=True
        ),
    )
    p.edge("start", "a")
    p.edge("start", "b", when=lambda s: False)
    p.edge("start", "c")
    p.edge("a", "done")
    p.edge("b", "done")
    p.edge("c", "done")
    run = await run_plan(p)
    assert run.state == RunState.COMPLETED
    assert run.final_output == "quorum-reached"
    assert run.state_of("b").status.value == "skipped"


# —— Goto's immediate-activation bypass is one-shot: it must not survive a later rearm ——
async def test_activated_bypass_is_one_shot_not_permanent():
    p = Plan()
    p.add(
        Node("slow", FnBody(lambda x, ctx: Outcome.ok("S"))),
        Node("gate", FnBody(lambda x, ctx: Outcome.ok("G")), join="all"),
    )
    p.edge("slow", "gate")
    run = Run.start(p)
    run.reset_pending("gate")
    assert "gate" in p.ready(run)
    run.mark_running("gate")
    run.mark_completed("gate", "G-first")
    run.rearm("gate")
    assert "gate" not in p.ready(run)  # activated was consumed; slow hasn't finished yet
    run.mark_running("slow")
    run.mark_completed("slow", "S")
    assert "gate" in p.ready(run)
