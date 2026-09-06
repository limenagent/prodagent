# Design note 01: why the blueprint and an execution must be separate

## What happens if you don't separate them

In a first framework, many people put the "flow chart" and the "running state"
into one object: a node carries both its connections and "how far I got this
time". It looks convenient, but you hit two walls fast.

First, **no reuse**. You'll likely run many user sessions against the same flow
chart. If state lives on the graph, the second session starts seeing traces of
the first, and you end up copying the whole graph per session.

Second, **no clean recovery**. "What the graph looks like" doesn't change; "how
far this run got" changes constantly. Mix them and you can't tell, at save time,
which part is the permanent blueprint and which is this run's temporary progress.

## After the split, everything has its place

So we split into two objects:

- **Plan (the blueprint)** answers static questions: which nodes, how they
  connect, which state channels. It is immutable, reusable, and open to static
  analysis (e.g. detecting a dangling edge). One Plan can be reused by thousands
  of Runs at once.
- **Run (one execution)** answers dynamic questions: how far this run got, each
  node's status, the current shared data. It is one-shot and serializable; on
  crash you recover from its snapshot.

A useful analogy: the Plan is the score, a Run is one particular performance.
The same score is performed countless times; each performance has its own
progress and rendition, and a bad night affects only that show, not the score.

## Capabilities this separation gives you for free

Once drawing and performance are split, several hard things become natural:

- **Concurrency**: many Runs share one read-only Plan with independent state, so
  they can't leak into each other.
- **Recovery**: snapshot only the Run and reload the Plan — clean responsibilities.
- **Sub-flow reuse**: a node's body can be "run another blueprint", because a
  blueprint is already independent, nestable data.
- **Visualization and validation**: the blueprint is plain data you can draw and
  check before running ("this edge points to a node that doesn't exist").

## One easy-to-miss detail

The blueprint is immutable in its *structure*; but a node's `body` (the function
that does the work) is an injected executable. In other words, the blueprint
describes only the skeleton; it doesn't weld a specific model vendor or tool
implementation into itself. That reserves room for port injection and offline
testing later.

## In the code

- `src/kernel/graph.py`: `Plan / Node / Edge` and the pure `ready()`.
- `src/kernel/run.py`: the Run's runtime state, state machine, and
  `snapshot()/restore()`.

> Why state belongs on the Run rather than on an Agent object, and exactly what a
> snapshot should and shouldn't store, are developed against the code in the
> companion column. Next, a more counter-intuitive and crucial decision:
> [why state is folded from events](02-state-from-events.md).
