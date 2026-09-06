# Design note 03: the wave as a consistency boundary

## What happens if nodes run whenever they like

Imagine three nodes running concurrently, all free to read and write the same
shared state at any moment. A writes halfway, B reads that half-finished state,
and C decides based on what B read. You get the hardest class of bug: **the
result depends on scheduling order, flips between runs, and can't be reproduced
reliably.** The model is already uncertain enough; the framework shouldn't add a
second layer of uncertainty at the execution layer.

## Advance wave by wave: gather first, commit together

So the scheduler uses waves (essentially BSP, bulk-synchronous parallel), three
steps per wave:

```text
1) ready: along the edges, compute the set of nodes whose prerequisites are now met
2) run concurrently: this wave's nodes run together, but none touches shared state — each only produces an Outcome
3) barrier: wait for all of them, then fold state deltas, apply control commands, write one checkpoint
```

The key is step 2: while running, shared state is a **read-only snapshot** to a
node; to change it the node returns a `state_delta`. The actual merge happens at
the step-3 barrier, folded deterministically by reducers. As a result:

- nodes in a wave never see each other's half-finished work — **what they read
  is always the consistent state at the previous wave's end**;
- merge order doesn't matter, the result is deterministic;
- every barrier is naturally a **commit point**: this wave either takes effect
  completely or not at all, and the checkpoint lands exactly here.

Dynamic fan-out fits the same rhythm: a node produces `Send`s in step 2, and
those new instances enter the **next** wave's ready set instead of cutting in
mid-wave, so order is preserved.

## The honest cost: it isn't free

The wave model isn't all upside. The clearest cost is the **straggler**: if one
node in a wave takes ten minutes, the others, however early they finish, wait at
the barrier before the next wave can start. Also, concurrency is bounded to
"nodes ready at the same level", which doesn't suit fine-grained, continuous
peer-to-peer communication.

This is a clear-eyed trade-off: for the vast majority of agent workflows,
**deterministic results, easy recovery, and easy debugging are worth far more
than maximal concurrency throughput.** When node durations differ wildly, split
the slow node finer or isolate it in a child Run rather than tearing down the
consistency boundary.

## In the code

- `drive()` in `src/kernel/scheduler.py`: the main loop is exactly those three
  steps, short enough to read as a direct translation of this figure.
- `ready()` in `src/kernel/graph.py`: purely computes who is ready each wave.
- `WaveWrites` in `src/kernel/channels.py`: buffers this wave's deltas and folds
  them once at the barrier.

> Why "fold state, apply Goto/Send, write checkpoint" must happen in exactly
> that order at the barrier is taken apart line by line in the companion column.
> Next, a decision about *doing less*: [why there is no ReAct in the
> kernel](04-no-pattern-in-kernel.md).
