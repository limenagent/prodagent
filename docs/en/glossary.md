# Glossary

Ordered roughly as you meet them while reading; each entry gives one plain
sentence and where it lives in the code.

## Kernel mechanisms

**Plan (blueprint)**: a static execution chart stating which nodes exist, how
they connect, and which state channels exist. Reusable and analyzable. See
`kernel/graph.py`.

**Node**: one step in the graph. It's just a slot; its body does the actual work.

**Edge**: a directed link between nodes, optionally with a `when` condition. An
edge expresses "after whom is a node eligible".

**Channel (state channel)**: a named state field plus a merge rule. Concurrent
writes to the same field merge deterministically by that rule instead of
clobbering each other.

**reducer**: a pure function `(old, new) -> merged`. Four built-ins: `last`
(last-write-wins), `append` (into a list), `add` (numeric sum), `merge` (dict merge).

**Run (one execution)**: one concrete run of a Plan, carrying current state, how
far each node got, and parent/child relations. One-shot and serializable.

**body (node executable)**: the part of a node that does the work — a plain
function, a tool call, an LLM call, or a recursively activated sub-plan. The
kernel only knows the single interface "it can be scheduled and run".

**Outcome**: what a body produces, split orthogonally into a downstream `value`,
a `state_delta`, a `control` command, and a `suspend` request.

**Command (control command)**: how a node tells the scheduler where to go next.
Just two: `Goto` re-arms a node (back-edges, jumps, handoffs), and `Send`
instantiates a template node at runtime (dynamic fan-out).

**Scheduler**: the single engine that loops over three things — compute
readiness, run a wave concurrently, fold at the barrier and checkpoint.

**ready set**: the nodes whose prerequisites are all satisfied and that can run
immediately in the current wave.

**wave / superstep**: "gather a batch → run concurrently → commit together at
the barrier" is one wave. The wave is a consistency boundary.

**barrier**: the synchronization point where a whole wave has finished; state is
folded and commands applied there.

**Event**: an immutable, append-only "fact that happened". Current state is a
projection folded from the event stream.

**EventLog**: the append-only, sole source of truth for events. Audit, recovery,
and time travel all work by replaying it.

**Checkpoint**: a snapshot of a Run taken at a wave boundary for fast recovery;
it is a disposable cache — the truth still lives in the events.

**Bus (event bus)**: the kernel's single outward seam with two protocols —
`fire` to observe, `check` to adjudicate; bounded
subscriptions additionally support streaming and backpressure.

**backpressure**: when a downstream consumer can't keep up, push pressure back
upstream (`block` and wait) or drop frames and count them (`drop`), instead of
letting memory grow without bound.

**Interrupt (suspend)**: at any point a node asks to "stop and wait for a human
or the outside world"; the framework persists and lets go. Later an external
`resume` gives it a push and it continues from the breakpoint.

**Port**: the protocol boundary between kernel and outside world (model, tools,
sub-agent, storage). The kernel knows only ports; implementations are injected
from outside and swapped for scripted Fakes in tests, so runs are offline and
deterministic.

## Upper-layer strategies

**ReAct**: the "think a step, do a step, think again" loop. Here it isn't a
kernel class but a recipe of two nodes (think/act) plus a back-edge.

**plan-first (plan then execute)**: the model first produces a step list (just
data in state, not the execution graph), then fans out workers and synthesizes.

**call (delegation)**: a parent activates a child Run that returns its result
when done and the parent continues; control always returns.

**transfer (handoff)**: in the same graph, `go` to another agent node with no
return edge, so control leaves and never comes back.

**blackboard**: agents don't call each other directly but co-write a set of
shared channels; whoever finds the data ready acts.

**Agent / Workflow (facade)**: ergonomic wrappers over kernel primitives. An
Agent is an object that thinks, uses tools, and delegates; a Workflow is a
declarative flow chart. Use the facade for 90% of cases and sink to primitives
for deep customization.
