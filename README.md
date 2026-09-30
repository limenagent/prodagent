# prodagent: an agent-framework kernel in 1800 lines

[![CI](https://github.com/limenagent/prodagent/actions/workflows/ci.yml/badge.svg)](https://github.com/limenagent/prodagent/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![runtime deps: 0](https://img.shields.io/badge/runtime%20deps-0-brightgreen.svg)]()
[![tests: offline](https://img.shields.io/badge/tests-offline-blue.svg)]()

English · [中文](README.zh-CN.md) · Docs: [English](docs/en/README.md) · [中文](docs/zh/README.md)

prodagent is a **teaching-grade agent runtime kept deliberately small**: six
parts, each with one job; about 1800 net lines; **zero third-party runtime
dependencies**; a test suite that runs entirely offline. Read it over a weekend
and you'll see how ReAct, plan-then-execute, and multi-agent collaboration are
all *composed* from the same primitives — and going back to LangGraph or Google
ADK gets much easier.

## Same skeleton, different clothes

Every mainstream framework ends up making the same small set of decisions, just
under different names. Learn prodagent's six parts and you know where to look
in any of them:

| Concern | prodagent (this repo) | The same idea where you already know it |
|---|---|---|
| a reusable blueprint of steps | `Plan` = Node / Edge / Channel | LangGraph `StateGraph`; ADK workflow / agent graph; CrewAI Process + Tasks |
| one execution and its state | `Run` + channels & reducers | LangGraph State + checkpointer; ADK Session |
| the engine that drives it | `Scheduler` recomputes a ready-set each wave (BSP) | LangGraph Pregel super-steps; ADK Runner; CrewAI's kickoff loop |
| runtime routing & fan-out | `Goto` / `Send` | LangGraph `Command(goto/send)`; ADK transfer; OpenAI handoffs |
| pause for a human | `Interrupt`, then resume | LangGraph `interrupt()` + `Command(resume)`; ADK human input |
| truth, replay, time travel | append-only `EventLog`; state is a fold | LangGraph checkpointer + time travel; ADK session replay |
| trace & output files | `build_trace`; `BlobStore` + pointer events | OpenTelemetry spans; ADK session artifacts |
| multi-agent | child Run (call) / no-return `Goto` (transfer) / blackboard | LangGraph subgraphs + `Send`; ADK sub-agents & transfer; CrewAI hierarchy |
| visible & interceptable from outside | `Bus`: observe / adjudicate / subscribe | LangGraph callbacks & stream; ADK EventBus |

No magic, nothing hidden: the kernel is 14 files you can read in a weekend.

## The shape

```mermaid
flowchart TB
  subgraph APP["Application (strategy): ReAct · plan-first · multi-agent · you"]
  end
  subgraph K["Kernel (mechanism) — six parts"]
    P["Plan: Node / Edge / Channel"] --> R["Run: one execution"] --> S["Scheduler: ready → wave → fold"]
    S --> L["EventLog: source of truth"]
    S --> BI["Bus / Interrupt"]
  end
  APP -->|assembled from the same primitives| K
```

**Mechanism inside, strategy outside.** There is no ReAct class and no
"execution-mode enum" in the kernel — every pattern is assembled on top from the
same primitives, and a new orchestration needs no kernel change.

## Try it in 30 seconds — no API key, no spend

```bash
git clone https://github.com/limenagent/prodagent && cd prodagent
pip install -e .

prodagent run            # offline end-to-end flow; answer its approval prompt
prodagent run --trace    # the same run as a tree of parent/child Runs
make play                # browser playground: trace, events, state, files, graph
```

Every example is driven by a *scripted* model that plays its part from a
script — fully offline, fully deterministic, run it as often as you like. Set
`OPENAI_API_KEY` to swap in any OpenAI-compatible model and nothing else
changes. More scenarios (plan-first, blackboard, log-based resume, backpressure,
long-term memory) live under `examples/`.

## The API in 15 lines

```python
from src import Agent, Workflow, go, send, wait_human

# an autonomous agent: model + tools
agent = Agent(name="researcher", model=llm, instruction="...", tools=[search])
await agent.run("look up X")

# a deterministic graph / handoff — a node is a function or a whole Agent
wf = Workflow()
wf.add_node("diagnose", diagnose_fn)
wf.add_node("repair", repair_agent, terminal=True)
wf.add_edge("diagnose", "repair")
wf.entry("diagnose")
await wf.run("incident")
```

A node is just an `async def fn(input, ctx)`: `return` a value and it goes
downstream. To route, `go(target, value)` — loops, back-edges, and handoffs are
all the same call (no return edge = transfer, control never comes back). To fan
out, `return [send("template", x) for x in items]` — however many copies the
data says, all concurrent in one wave. To wait for a person,
`wait_human("question")` — the question and the later answer are both facts in
the log, and the run resumes from them.

## Go deeper

- **New here? Start with [build a minimal kernel in 30 minutes](docs/en/build-a-minimal-kernel.md)** — 80 lines of stdlib, type it once and it clicks.
- [Architecture: derive the six parts from a six-line loop](docs/en/architecture.md)
- [Five key design trade-offs](docs/en/README.md) · [Framework comparison](docs/en/comparison.md) · [FAQ](docs/en/faq.md) · [Glossary](docs/en/glossary.md)
- The kernel's module reading order is written in the header of `src/__init__.py` (types → channels → graph → … → scheduler); run the tests with `python -m pytest tests/ -q`.

If this helps you actually understand agent frameworks instead of memorizing
APIs, a **GitHub Star ⭐** helps other engineers find it too.
