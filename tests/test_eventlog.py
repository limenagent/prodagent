"""Event sourcing: state is a projection folded from the event stream; snapshots are serializable and restorable."""

from src.kernel import (
    FnBody,
    Node,
    Outcome,
    Plan,
    RunState,
    Scheduler,
    add,
    append,
    fold_events,
)


async def test_state_equals_fold_of_event_stream():
    p = Plan(channels={"log": append(), "n": add(0)})
    p.add(
        Node("a", FnBody(lambda x, ctx: Outcome.ok(None, log=["a"], n=1))),
        Node("b", FnBody(lambda x, ctx: Outcome.ok(None, log=["b"], n=2)), terminal=True),
    )
    p.edge("a", "b")
    sch = Scheduler()
    run = await sch.run(p)

    events = await sch.eventlog.events(run.run_id)
    deltas = [e for e in events if e.kind == "state_delta"]
    assert deltas, "state-delta events should have been recorded"

    # replaying the whole delta stream from empty state must reproduce the run's shared state.
    rebuilt = fold_events(deltas, p.channels, p.initial_shared())
    assert rebuilt == run.shared


async def test_run_end_is_recorded_in_the_event_stream():
    """Both ways a run can end land in the log: replaying the stream can
    reconstruct not only the state, but the fact that the run ended."""
    p = Plan().add(Node("a", FnBody(lambda x, ctx: "done"), terminal=True))
    sch = Scheduler()
    run = await sch.run(p)
    kinds = [e.kind for e in await sch.eventlog.events(run.run_id)]
    assert kinds[-1] == "run_completed"

    cyc = Plan()
    cyc.add(
        Node("a", FnBody(lambda x, ctx: Outcome.ok("A"))),
        Node("b", FnBody(lambda x, ctx: Outcome.ok("B"))),
    )
    cyc.edge("a", "b")
    cyc.edge("b", "a")  # entry-less cycle: neither can start, the graph stalls
    stalled = await sch.run(cyc)
    assert stalled.state == RunState.FAILED
    kinds = [e.kind for e in await sch.eventlog.events(stalled.run_id)]
    assert kinds[-1] == "run_failed"
