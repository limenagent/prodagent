# prodagent: an agent framework you can read end to end — and start hacking on

**English** · [中文](README.md) · Companion framework for the GeekTime column
[《生产级 Agent 排雷实战》](http://gk.link/a/12L6Q) (in Chinese)

prodagent is a **teaching-oriented framework that still keeps the capabilities production
actually needs**. With as few and as orthogonal abstractions as possible, it answers one
question: *what pieces make up an agent execution engine, and why exactly those?* You can
read the whole kernel and rewrite it yourself; then use the recipes above it to assemble
ReAct, plan-first execution, multi-agent collaboration, and more.


## Three layers

```
examples/          business examples: ReAct, approval, compaction, multi-agent, MCP
─────────────────────────────────────────────────────────────
src/runtime/       policy/recipe layer (replaceable as a whole)
  react / plan_first / multiagent        how to orchestrate
  tools / mcp                            where tools come from
  context / memory / skills              cross-cutting policies, injected as needed
src/backends/      storage implementations: file-level checkpoint & resume
─────────────────────────────────────────────────────────────
src/kernel/        mechanism layer (zero third-party deps, knows nothing about "modes")
  Plan / Run / Scheduler / Node / Edge / Channel
  Outcome / Command / Interrupt / Bus / EventLog

  ▲ models / tools / sub-agents / storage are injected through ports; the kernel never imports them
```

**Mechanisms inside, policies outside.** There is no ReAct in the kernel and no "execution
mode enum"; ReAct, plan-first, and multi-agent are all assembled on top of the same kernel
primitives. Swapping in a different orchestration never touches a line of the kernel.

## One diagram of the kernel

```
application layer (policies)
  ReAct recipe · plan-first · multi-agent · your business …
        │  all assembled from the kernel primitives below
        ▼
kernel (mechanisms)
  Plan (static blueprint): Node / Edge / Channel
  Run (one dynamic execution): state / instances / lifecycle
        │
        ▼
  Scheduler (the engine): compute ready → wave concurrency → barrier fold → checkpoint
  Outcome / body · Command (Goto/Send)
  Interrupt (suspension) · Bus (three protocols + backpressure) · EventLog (source of truth)
```

> If you find this useful, consider
> [giving it a Star ⭐ on GitHub](https://github.com/limenagent/prodagent) —
> your support helps more agent engineers fighting fires in production discover it.

## Kernel parts, each in its own file

| Part | File | Responsibility in one sentence |
|---|---|---|
| Plan / Node / Edge | `kernel/graph.py` | the static blueprint, plus the pure computation of "who is ready this wave" |
| Run | `kernel/run.py` | the dynamic state of one execution, its lifecycle state machine, snapshots |
| Channel / reducer | `kernel/channels.py` | how concurrent writes merge deterministically (append/last/add/merge) |
| Outcome / body | `kernel/body.py` | the one composable interface + four bodies: function/tool/model/subgraph |
| Command | `kernel/command.py` | Goto / Send: only change "the ready set of the next wave"; Goto can carry a payload as the transition input |
| EventLog / Store | `kernel/eventlog.py` | events are the source of truth, state is a folded projection |
| Bus | `kernel/bus.py` | observe fire / arbitrate check / collect collect + bounded-subscription backpressure |
| Scheduler | `kernel/scheduler.py` | the BSP wave main loop that assembles all parts into one machine |
| ports | `kernel/ports.py` | dependency-inversion ports for LLM / tools / sub-agents |

### Three running principles

1. **State is a projection folded from an event stream.** Nodes never touch shared state
   directly; they only emit `state_delta`. The engine folds it per reducer at the wave
   barrier and appends the wave delta to the event log. Replay is rebuild — audit, time
   travel, and crash recovery all become the same thing.
2. **A wave is the consistency boundary.** Nodes in the same wave run concurrently and
   never see each other's half-done work; everything commits together at the end of the
   wave, so results are independent of scheduling order. Every wave boundary is naturally
   a checkpoint.
3. **Complex capabilities grow out of recursive composition of primitives.** Multi-agent
   is not a new engine: call (delegation) is a node body recursively running a child Run
   and handing the result back; transfer is even cheaper — in the same graph, `go` to the
   other agent's node and draw no back edge, and control never returns. Different ways of
   rejoining, same single Goto.

## Two ways to use it: the facade, or the kernel directly

**Most of the time the facade is enough** — `Agent` is an autonomous unit that thinks,
calls tools, and delegates to teammates; `Workflow` is a flowchart you can actually read,
whose nodes can be plain functions or whole Agents:

```python
from src import Agent, Workflow, go

# 1) An autonomous agent: model + tools, just run it
agent = Agent(name="researcher", model=llm, instruction="...", tools=[search])
result = await agent.run("look up X for me")  # result.output is the final reply

# 2) A boss delegating: teammates are sub-agents called away that return results (call)
boss = Agent(name="boss", model=llm, teammates=[researcher, writer])

# 3) Deterministic orchestration / multi-agent transfer: Workflow
async def decide(root, ctx):
    return go("repair", root)  # transition to the repair agent: no back edge = no coming back

wf = Workflow()
wf.add("diagnose", diagnose_fn)  # a function node
wf.add("decide", decide)  # a plain node deciding where to transition
wf.add("repair", repair_agent, terminal=True)  # a node can also just be an Agent
wf.edge("diagnose", "decide")
wf.entry("diagnose")
result = await wf.run("incident")
```

Control flow inside nodes uses three memorable functions: `go` (transition — back edges,
loops, and handovers alike; the value becomes the target's input for that run), `send`
(dynamic fan-out — `return [send("worker", x) for x in items]`; the count may only be
known at runtime, the engine runs all instances concurrently in one wave), and
`wait_human` (park and wait for a human, then `wf.resume`). To see how the facade is
assembled from Plan/Node/Scheduler underneath, go back to the kernel plus
`graph_demo.py` and `react_demo.py`.

## Recipes and cross-cutting policies on top (all replaceable)

| Capability | Where | Notes |
|---|---|---|
| Agent / Workflow facade | `runtime/agent.py`, `runtime/workflow.py` | the friendly high-level API: autonomous agents, declarative graphs, go/send/wait_human |
| ReAct | `runtime/react.py` | think⇄tools loop + final; forward motion on the loop is driven by Goto, unbounded tool rounds |
| Plan-first | `runtime/plan_first.py` | the LLM plan is just a step list in state; send fans out dynamically, the synthesis node waits for all predecessors |
| Multi-agent | `runtime/multiagent.py` | pipeline / supervisor (sub-agents as tools) / blackboard (experts write in parallel, a join=all moderator adjudicates, multiple rounds converge); transfer = `go` within the same graph, no back edge |
| Tools | `runtime/tools.py` | functions as tools with inferred schemas, read/write tiers, approval gates, failures fed back as results |
| MCP | `runtime/mcp.py` | MCP tools flattened into ordinary tools at the boundary; one pipeline inside |
| Context | `runtime/context.py` | five-tier compaction: leave alone → mechanically shrink tool results → summarize tier by tier → emergency recent-only; the assembly strategy is swappable |
| Long-term memory | `runtime/memory.py` | one unified record + orthogonal tags, swappable retrieval (keyword-based teaching version; swap in vectors for production) |
| Skills | `runtime/skills.py` | tools + operating instructions packaged as expertise; loads from a directory's SKILL.md, chosen on demand |
| Step elasticity | `kernel/graph.py`, `kernel/scheduler.py` | Node carries timeout + RetryPolicy; a timeout counts as one failure, retried with exponential backoff |
| Streaming backpressure | `kernel/bus.py` | nodes `ctx.emit` while computing; bounded subscriptions block (backpressure) or drop with accounting |
| File persistence | `backends/file_store.py` | atomic checkpoint writes + JSONL events; resume across processes |

## Examples, from shallow to deep

```bash
cd src
PYTHONPATH=. python examples/graph_demo.py        # how waves advance (pure kernel)
PYTHONPATH=. python examples/react_demo.py        # ReAct assembled by hand (pure kernel)
PYTHONPATH=. python examples/01_greeter.py        # minimal agent: ReAct with one tool
PYTHONPATH=. python examples/02_trader.py         # multi-round bargaining + write approval gate + memory
PYTHONPATH=. python examples/03_deep_research.py  # many rounds of lookup + five-tier context compaction
PYTHONPATH=. python examples/04_compliance_audit.py # parallel checks + suspended approval; rejection doesn't tear it down
PYTHONPATH=. python examples/05_code_detective.py # MCP tools + skills loaded from disk + retry with changes
PYTHONPATH=. python examples/06_trip_planner.py   # a main agent fanning out three parallel sub-agents
PYTHONPATH=. python examples/07_aiops.py          # diagnosis via call (must return) + repair via transfer (go, no coming back)
PYTHONPATH=. python examples/09_persistence.py    # checkpoints on disk; a fresh instance resumes from the breakpoint
PYTHONPATH=. python examples/10_retry_timeout.py  # node timeouts + exponential backoff retry
PYTHONPATH=. python examples/11_backpressure.py   # nodes streaming events; bounded subscriptions block/drop
```

Every example uses `ScriptedLlm` to play the model from a script — **they run offline**,
with no real API required.

## Playground: one command, every example in the browser

```bash
make play                 # equivalent: PYTHONPATH=. python3 -m src.playground
# open http://127.0.0.1:8000
```

Pick any of the 9 scenarios on the left (including multi-agent draft–review–revise,
parallel delegation + transfer, cross-session memory recall). On the right you get:

- a **timeline of the event stream** (node started/completed, state deltas, run finished —
  subscribed from the very same Bus);
- human-in-the-loop nodes using `wait_human` **genuinely suspend**; the page shows an
  approve/reject prompt and continues from the breakpoint once you answer;
- multi-agent parallelism, delegation (call), and transfer are all visible on the timeline.

The Playground uses only the standard library (`http.server` + a background event loop) —
no web framework. Scenarios default to the offline scripted model, so it's zero-config.

### Plugging in a real model (optional)

`runtime/openai_lite.py` talks to any OpenAI-compatible service over the standard library,
no SDK. Set the environment variables `OPENAI_API_KEY`, optionally `OPENAI_BASE_URL`
(self-hosted gateways / compatible services), and `OPENAI_MODEL`, then swap the scripted
model for it — nothing else in your code changes:

```python
from src.runtime.openai_lite import OpenAICompatibleLlm

agent = Agent(name="demo", model=OpenAICompatibleLlm(), tools=[...])
```

## Running the tests

```bash
pip install pytest pytest-asyncio
python -m pytest tests/ -q
```

## Suggested reading order

1. `kernel/types.py → command.py → channels.py`: value objects and merge rules;
2. `kernel/graph.py`: the static blueprint and `ready()`, the pure function most worth a
   careful read in the whole kernel;
3. `kernel/run.py → body.py`: what one execution carries, and what the single composable
   interface looks like;
4. `kernel/eventlog.py → bus.py → ports.py`: source of truth, the outward seam,
   dependency inversion;
5. then `kernel/scheduler.py`: the main loop is short enough to feel like a recap;
6. move on to `runtime/`: watch the same primitives assemble ReAct and multi-agent;
7. cross-reference `examples/` and `tests/`, and you're ready to change things yourself.
