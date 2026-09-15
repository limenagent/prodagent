# Build a minimal kernel in 30 minutes

Reading about nodes, waves, and ready-sets can feel clear in the moment and
vanish as soon as you close the tab. The fix is to **build the tiniest possible
engine once yourself**. This page does not import prodagent at all — using only
the Python standard library, in about 80 lines, you will write a working engine
that runs a graph in concurrent waves. After that, the real kernel stops looking
like a pile of names.

You only need to know what an `async def` function is. Follow the five steps; the
full runnable script is collected at the end.

## The three smallest parts

Even a toy engine needs three things, and they are exactly the first three parts
of the real kernel:

- a **Plan** — which nodes exist and how they connect (static, reusable);
- a **Run** — how far *this* execution got and the data it produced (dynamic, one-shot);
- a **Scheduler** — something that keeps asking one question: *which nodes are ready now?*

## Step 1 — a node is just an async function

A node does a piece of work and returns a value. It may be `async` because real
nodes wait on models, tools, or network:

```python
async def a(shared):
    return "done by a"
```

`shared` is the data accumulated so far; we will see how a node is only allowed to
read results from earlier waves.

## Step 2 — the Plan records nodes and edges

The blueprint keeps each node's body and each node's predecessors. Edges are just
"who must finish before me":

```python
class Plan:
    def __init__(self):
        self._bodies = {}          # node name -> body
        self._pred = {}            # node name -> predecessor names

    def add(self, name, body):
        self._bodies[name] = body
        self._pred.setdefault(name, [])
        return self                # return self so calls can chain

    def edge(self, src, dst):
        self._pred.setdefault(src, [])
        self._pred.setdefault(dst, []).append(src)
        return self
```

`setdefault(key, [])` is a plain-Python idiom: "use the existing list if there is
one, otherwise start an empty one." Nothing framework-y here.

## Step 3 — the Run records one execution's progress

A blueprint is shared by every request; a Run belongs to exactly one. It holds
each node's status and the shared data this execution has produced:

```python
PENDING, COMPLETED = "pending", "completed"

class Run:
    def __init__(self):
        self.states = {}           # node name -> PENDING / COMPLETED
        self.shared = {}           # node name -> its returned value
```

## Step 4 — `ready()` is the one thing the engine computes

Here is the heart of the whole engine, in four lines: a node is **ready** when it
has not run yet, and every one of its predecessors has completed:

```python
def ready(self, run):
    out = []
    for name in self._bodies:
        if run.states.get(name, PENDING) != PENDING:
            continue                       # already done
        if all(run.states.get(p) == COMPLETED for p in self._pred[name]):
            out.append(name)               # every predecessor finished -> ready
    return out
```

A node with no predecessors has an empty predecessor list; `all(...)` over an empty
list is `True`, so entry nodes are ready in the very first wave. Note this is a
**pure function of the current Run** — it does not run anything, it only answers
"who is ready". The real `graph.py` is the same computation with condition edges,
fan-out, and joins added.

## Step 5 — the Scheduler advances wave by wave

The engine loops: ask for the ready set, run that whole wave **concurrently**,
wait for all of it at a barrier, commit results, and repeat. It stops when no node
is ready:

```python
class Scheduler:
    async def drive(self, plan, run):
        wave = 0
        while True:
            ready = plan.ready(run)
            if not ready:
                break                        # nobody left to run -> done
            wave += 1
            print(f"wave {wave}: {ready}")

            async def run_node(name):
                value = await plan._bodies[name](run.shared)
                run.shared[name] = value     # commit only here, at the barrier
                run.states[name] = COMPLETED

            await asyncio.gather(*(run_node(n) for n in ready))
        return run.shared
```

Why commit at the barrier instead of the instant a node finishes? Because nodes in
the same wave run at the same time and must never see each other's half-finished
result. Everyone reads only the previous waves' committed data, then the whole
wave commits together — so the outcome never depends on which node happened to
finish first. This single rule is what makes concurrent execution deterministic.

## Put it together and run it

Here is the complete script — save it as `mini.py` and run `python mini.py`, no
install, no API key:

```python
import asyncio

PENDING, COMPLETED = "pending", "completed"


class Plan:
    def __init__(self):
        self._bodies = {}
        self._pred = {}

    def add(self, name, body):
        self._bodies[name] = body
        self._pred.setdefault(name, [])
        return self

    def edge(self, src, dst):
        self._pred.setdefault(src, [])
        self._pred.setdefault(dst, []).append(src)
        return self

    def ready(self, run):
        out = []
        for name in self._bodies:
            if run.states.get(name, PENDING) != PENDING:
                continue
            if all(run.states.get(p) == COMPLETED for p in self._pred[name]):
                out.append(name)
        return out


class Run:
    def __init__(self):
        self.states = {}
        self.shared = {}


class Scheduler:
    async def drive(self, plan, run):
        wave = 0
        while True:
            ready = plan.ready(run)
            if not ready:
                break
            wave += 1
            print(f"wave {wave}: {ready}")

            async def run_node(name):
                value = await plan._bodies[name](run.shared)
                run.shared[name] = value
                run.states[name] = COMPLETED

            await asyncio.gather(*(run_node(n) for n in ready))
        return run.shared


# graph: a -> (b, c) -> d
async def a(shared): return "done by a"
async def b(shared): return f"b read [{shared['a']}]"
async def c(shared): return f"c read [{shared['a']}]"
async def d(shared): return f"d merged [{shared['b']}] & [{shared['c']}]"

plan = Plan()
(plan.add("a", a).add("b", b).add("c", c).add("d", d))
plan.edge("a", "b").edge("a", "c").edge("b", "d").edge("c", "d")

final = asyncio.run(Scheduler().drive(plan, Run()))
print("final shared:", final)
```

The output shows exactly three waves — `a` alone, then `b` and `c` together, then
`d`:

```text
wave 1: ['a']
wave 2: ['b', 'c']
wave 3: ['d']
final shared: {'a': 'done by a', 'b': 'b read [done by a]', 'c': 'c read [done by a]', 'd': 'd merged [b read [done by a]] & [c read [done by a]]'}
```

Change an edge, add a node, or make `b` slow with `await asyncio.sleep(1)` and
watch the wave shape change. That is the whole feedback loop of an engine.

## From these 80 lines to prodagent

You have just written the skeleton. The real kernel is not a different idea — it is
this skeleton with one capability added per problem you would hit next:

| This toy | prodagent adds | because otherwise |
|---|---|---|
| nodes write `shared` directly | named channels + reducers, nodes return `state_delta` | concurrent writers can overwrite each other |
| when the process exits, everything is gone | an append-only `EventLog`; state is a fold of events | no crash recovery, replay, or time travel |
| runs straight to the end | `Interrupt`: persist, let go, resume later | you cannot pause for human approval |
| runs silently | a `Bus` to observe, adjudicate, and subscribe | nothing outside can see or guard execution |
| edges are fixed up front | `Goto` to choose an edge at runtime, `Send` to fan out | loops, handoffs, and runtime-sized fan-out are impossible |
| a body is just a function | four bodies (fn / tool / LLM / sub-plan), model behind a port | the kernel would have to know one specific vendor |

Each row is one part of the six, and each is derived the same way you just derived
the first three: hit a concrete wall, add the smallest thing that removes it, and
nothing more. The companion column walks through every one of those steps with the
real code.

## Where to go next

- [Architecture overview](architecture.md) — see all six parts on one figure.
- [Design note 01](design/01-plan-and-run.md) — why the Plan/Run split you just wrote is mandatory, not stylistic.
- Then read `src/kernel/graph.py` and `src/kernel/scheduler.py` — you will recognize `ready()` and the wave loop immediately.
