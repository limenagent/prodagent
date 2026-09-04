# prodagent

> **A production-grade Agent framework whose kernel is small enough to read whole, complete enough for production, and elegant enough to keep.**
>
> ~30k lines · 13 packages · 1,088 offline tests · only 4 core dependencies

[![Python](https://img.shields.io/pypi/pyversions/prodagent?logo=python&logoColor=white)](https://pypi.org/project/prodagent/)
[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-1%2C088-offline-green)]()
[![Dependencies](https://img.shields.io/badge/core%20deps-4-purple)]()

[中文](README.md) · **English**

---

## One line

The kernel is **seven parts**. Everything else is an application-layer recipe:

```
User layer (strategy):  ReAct / plan-and-resolve / multi-agent
              │ composed from kernel primitives
Kernel (mechanism):
  Plan = Node + Edge + State channel   ← static blueprint (code becomes edges)
  Run     one execution                ← state machine + parent-child
  Scheduler  readiness waves            ← the one engine
  Interrupt  let go and resume          ← durable pause
  Bus        fire / check / collect    ← the one seam out
  Event Log  the truth; State = fold(events)
```

The kernel knows no vendor and no "mode". ReAct, plan-first, multi-agent — all are **recipes composed from kernel primitives** at the application layer. `runtime/recipes/` (`LoopBody`, `react`) are the reference implementations. The framework never drafts an execution graph from a task: an Agent is ReAct by default (agent-as-unit); plan-and-resolve is composed by you, at the application layer, from kernel primitives (see `compliance_audit`).

---

## Three API levels, one graph

```
L1  prebuilt    ReActAgent / LoopBody —— one line, out of the box
L2  @workflow   @workflow decorator + sequence/if/while/parallel → edges
L3  raw graph   Plan / Node / Edge / Channel —— hand-written
```

All three run on the **same kernel**. Write L2 code, it compiles to the L3 graph, the scheduler doesn't change a line.

---

## 30 seconds

### Minimal (ReAct loop by default)

```python
import asyncio
from prodagent import Agent, tool

@tool(name="search", readonly=True)
async def search(query: str) -> str:
    return f"results for: {query}"

agent = Agent("demo", system_prompt="Find answers.", tools=[search])

asyncio.run(agent.chat("what's the weather in Paris?"))
```

### @workflow: write the flow as code, the compiler makes the edges

```python
from prodagent import workflow, compile

async def fetch(ctx, s): ...
async def analyze(ctx, s): ...
async def report(ctx, s): ...

@workflow
async def body(ctx, s):
    await ctx.call(fetch)            # sequence → sequence edge
    if s.need_deep:                  # if → conditional edge
        await ctx.call(analyze)
    await ctx.call(report)           # while → back edge

plan = compile(body).plan            # compiles to Plan(nodes, edges)
```

### Production armor in one line

```python
from prodagent import Agent, AgentConfig
from prodagent.base.config import production

agent = Agent("demo", tools=[search],
              config=AgentConfig(name="demo", framework=production()))
```

---

## Documentation

Design docs ([start here →](docs/book/ch00.md)):

| Ch | Topic |
|---|---|
| 0 | [5-minute quickstart](docs/book/ch00.md) |
| 1 | [The kernel's seven parts](docs/book/ch01.md) |
| 2 | [Model layer](docs/book/ch02.md) |
| 3 | [The loop](docs/book/ch03.md) |
| 4 | [Budget](docs/book/ch04.md) |
| 5 | [Tool system](docs/book/ch05.md) |
| 6 | [Memory, compression, skills](docs/book/ch06.md) |
| 7 | [Event log & crash recovery](docs/book/ch07.md) |
| 9 | [Approval](docs/book/ch09.md) |
| 10 | [Multi-agent](docs/book/ch10.md) |
| 11 | [Observability](docs/book/ch11.md) |
| — | [API reference](docs/book/api-reference.md) · [Appendix](docs/book/appendix.md) |

---

## Install

```bash
pip install prodagent

pip install "prodagent[openai,anthropic]"     # providers
pip install "prodagent[playground]"            # visual playground
pip install "prodagent[postgres,redis,neo4j]"  # production backends
```

---

## Examples

Seven runnable examples under `examples/`:

- `greeter` — minimal skeleton
- `trader` / `deep_research` / `code_detective` — ReAct loops
- `compliance_audit` — **plan-and-resolve from kernel primitives** + HITL approval
- `trip_planner` — ReAct orchestration + parallel spawn delegation + memory
- `aiops` — tool-level approval + orchestration

---

## License

AGPL-3.0-only — see [LICENSE](LICENSE).
