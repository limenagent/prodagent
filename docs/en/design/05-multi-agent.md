# Design note 05: why multi-agent needs no new engine

## The common approach: a whole extra set of things for "multi-agent"

At the word multi-agent, many frameworks add a batch of concepts: a supervisor
agent, sequential agent, parallel agent, loop agent, swarm — one class per
collaboration style, more and more to memorize. This mistakes an *upper-layer
collaboration strategy* for a *new mechanism the kernel must support*.

## Back to the essence: one agent calling another is just a node running another graph

Recall the earlier conclusion: a node's body can be anything — a function, a
tool, an LLM call. So it can of course be "**activate another blueprint and
recursively run a child Run**." That is the whole secret of multi-agent: **no new
engine — a sub-agent is a node body that recursively invokes the same kernel.**
Parent and child Runs sit on one Run tree, their events go to one log, and the
very same Scheduler runs them.

What you do need to understand clearly are two **orthogonal axes**; get them and
every collaboration pattern falls into place.

**Axis one — control flow: how control moves.**

- **call (delegation)**: a parent node hands a sub-task to a child Run; **the
  child finishes and returns its result, and the parent continues**. Return of
  control is structurally guaranteed — suited to "a supervisor dispatches work
  and synthesizes the replies".
- **transfer (handoff)**: in the same graph, `go` to another agent node with
  **no return edge**, so control leaves and never comes back; the next agent
  faces the user directly. It isn't a new command, just an ordinary `Goto`, with
  the handover summary in its payload. Who you can transfer to is structurally
  exactly "which agent nodes are registered on the graph".
- **blackboard**: agents don't call each other directly; they write to a set of
  shared state channels, and whoever finds "it's my turn, the data is ready"
  acts, driven by channel `when` conditions and join rules.

**Axis two — state: how data is shared.**

- **isolated by default**: a child Run has private state; parent and child
  explicitly declare what flows down and what returns up; nothing undeclared
  crosses the boundary. Clean and controllable.
- **shared blackboard**: several agents explicitly share a set of channels (e.g.
  a common message history), suited to handoff dialogue and experts co-writing
  one board.

## Every industry "pattern" is a fixed setting on the two axes

Once you look through the two axes, the intimidating names all land with zero
kernel additions:

| Industry name | Control-flow axis | State axis |
|---|---|---|
| pipeline | static sequential edges | isolated; upstream output mapped downstream |
| supervisor | parent dispatches via call, collects results | isolated |
| hierarchical | call nested in call; the Run tree grows depth naturally | isolated per layer |
| swarm handoff net | agents transfer among themselves (same-graph go, no return edge) | shared message channel |
| generator-critic | call out, result returns, possibly multi-round | isolated |
| blackboard | whoever has the data acts | shared channels + conditional triggers |

## Two engineering problems that must be structurally covered

Recursive composition is powerful, but two holes must be closed structurally
rather than trusting the model to "behave":

1. **Cycle prevention and depth limits**: A delegates to B and B back to A, and
   the Run tree only grows. Nested delegation is capped by a max depth
   (`max_depth`); same-level ping-pong is capped by a max wave count
   (`max_waves`); exceeding either fails outright.
2. **Failure propagation along the tree**: a child Run called out that fails
   can't be swallowed by the parent as a normal result — failure propagates up
   the Run tree; after a transfer, control (and cancellation authority) has
   already moved. These two semantics must be kept distinct.

## In the code

- `InProcessActivator` in `src/kernel/scheduler.py`: by default recursively runs
  a child Run in-process with the same scheduler; a remote implementation only
  has to satisfy the same port protocol, with no kernel change (location
  transparency).
- `SubPlanBody` in `src/kernel/body.py`: the "activate a sub-plan" body.
- `src/runtime/multiagent.py`: how pipeline / supervisor / blackboard recipes
  are assembled from primitives.

> Why delegation and handoff must be distinguished explicitly, how parent-child
> state mapping keeps a subgraph from polluting the parent, and how to write the
> full code of each collaboration pattern, are built out across several sections
> of the companion column. With this, the five key kernel trade-offs are covered;
> return to the [documentation guide](../README.md) for the cross-reference table.
