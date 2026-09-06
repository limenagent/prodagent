# Design note 02: why state is folded from events

## The intuitive approach: mutate a dict

The most natural idea is to let each node mutate shared state directly:
`state["cost"] += 1`, `state["messages"].append(...)`. That's fine serially on
one machine, but this kernel runs several nodes **concurrently in a wave**, and
two problems appear together.

First, **concurrent overwrite**: two nodes write the same key at the same time,
the later write clobbers the earlier, and the result depends on who finished
first — nondeterministic. Second, **no history**: you only hold the "current
final state", not how it got there — for audit, for going back ten minutes, or
for rebuilding after a crash, you're missing a record of the process.

## A different idea: record only facts, compute state any time

We solve both at once by **not storing state directly — only appending events**.
An event is an immutable "fact that happened", e.g. "a node finished and
produced these deltas". Events are append-only and never modified, forming an
EventLog. Current state is the result of **folding** that stream from the start
with merge rules (reducers):

```text
initial --event1--> s1 --event2--> s2 --event3--> ... --> current state
             fold          fold          fold
```

Concurrent overwrite is solved by declaring a reducer per state channel: the
messages channel *appends*, the cost channel *adds*, the current-phase channel is
*last write wins*. The engine folds once at the wave barrier, so the result is
deterministic regardless of finish order. A channel with no declared merge rule
that gets multiple writers in one wave raises outright, rather than silently
dropping data.

## One decision buys three things at once

Because state is a projection of the event stream, the same log hands you:

- **audit**: the event stream is the complete trace of how state got here;
- **crash recovery**: replay the stream to rebuild state — no separate mechanism;
- **time travel**: fold up to any event and stop — that's the state then.

These are no longer three features to build separately; they are three natural
consequences of the single decision "events are the source of truth".

## The cost, and how we cover it

Honestly, pure replay has a cost: a long run may accumulate thousands of events,
and folding from scratch on every resume is slow. The answer is **snapshot plus
incremental replay**: periodically store the folded result as a snapshot (a
disposable cache); on resume, start from the latest snapshot and replay only the
events after it. Delete every snapshot and you can still fully recover from the
event log alone — the truth always lives in the events.

## In the code

- `src/kernel/channels.py`: the four reducers (`last/append/add/merge`) and the
  wave-write buffer.
- `src/kernel/eventlog.py`: `Event`, `apply_event / fold_events`, the log and
  checkpoint protocols.
- `src/kernel/run.py`: `fold_writes()` folds "this wave's delta" into history.

> Why a reducer must be pure, and why the wave delta is aggregated inside the
> wave before entering the log (otherwise replay double-counts), are walked
> line by line through `fold_writes` in the companion column. Next:
> [the wave as a consistency boundary](03-waves.md).
