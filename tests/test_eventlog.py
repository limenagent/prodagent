"""Event sourcing: state is a projection folded from the event stream; snapshots are serializable and restorable."""

from src.kernel import (
    FnBody,
    Node,
    Outcome,
    Plan,
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


async def test_snapshot_roundtrip():
    p = Plan(channels={"log": append()})
    p.add(Node("a", FnBody(lambda x, ctx: Outcome.ok("A", log=["a"])), terminal=True))
    run = await Scheduler().run(p)
    snap = run.snapshot()
    import json

    blob = json.dumps(snap, ensure_ascii=False)  # a snapshot must be JSON-serializable
    restored = type(run).restore(p, json.loads(blob))
    assert restored.state == run.state
    assert restored.shared == run.shared
    assert restored.final_output == run.final_output
