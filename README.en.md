# prodagent

> **A production-grade LLM agent framework — small enough to finish reading, complete enough to run in production.**

[![PyPI](https://img.shields.io/pypi/v/prodagent?color=blue&logo=pypi&logoColor=white)](https://pypi.org/project/prodagent/)
[![Python](https://img.shields.io/pypi/pyversions/prodagent?logo=python&logoColor=white)](https://pypi.org/project/prodagent/)
[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/limenagent/prodagent/ci.yml?logo=github&label=CI)](https://github.com/limenagent/prodagent/actions)
[![Tests](https://img.shields.io/badge/tests-1%2C182-offline-green)]()
[![Dependencies](https://img.shields.io/badge/core%20deps-4-purple)]()

[中文](README.md) · **English** · Companion framework to the GeekTime column [《生产级 Agent 排雷实战》](http://gk.link/a/12L6Q)

---

## You can use LangChain — but can you design an agent system?

Scan 2026's agent job postings and the tiers are clear.

Junior roles ask for "proficient with LangChain/LangGraph, can write prompts, has done RAG" — three months to get there, so the supply is the most crowded. Senior and architect roles read completely differently: "build a production-grade agent system from 0 to 1", "deeply customize or build core modules in-house", "multi-agent architecture design". Two to three times the pay, and very few people can take it.

Behind these requirements is a complete ability to design agent systems. In interviews, nobody asks "how do you initialize LangChain's `AgentExecutor`" — they ask:

- How many layers of armor does a `while True` loop that calls a model need before it's production-ready?
- How do the four-axis budgets — turns / seconds / tokens / cost — all take effect at once, halting on the first breach?
- When a long task gets `kill -9` halfway, how do you resume from a checkpoint without losing state or re-executing steps?
- When a model calls a tool that doesn't exist, or passes wrong arguments, how do you prevent it?
- When should you split into multiple agents, and when is a single agent with good context management enough?
- You changed a prompt — how do you know whether it got better or worse?

These questions can't be answered by papers or API docs. The solutions are scattered across issue threads, cloud-vendor docs, and some open-source framework's source — you'd have to dig through hundreds of thousands of lines and piece it together yourself.

**This repo has already threaded it all together for you.**

---

## Why prodagent, not something else

Agent projects on the market sit at two extremes; prodagent is in the middle:

| | Black-box frameworks (LangChain / AutoGen) | Teaching toys (a few dozen lines of ReAct) | **prodagent** |
|---|---|---|---|
| Feature completeness | High | Low | **High** |
| Code readability | Thick abstraction layers, can't touch the core | Clear, but no production mechanisms | **25,000 lines, ordered for learning** |
| Budget / recovery / approval | None, or locked to the cloud | None | **Built in, on by default** |
| Core dependencies | Dozens (transitive) | 1–2 | **4** |
| Tests | Depend on real APIs | Almost none | **1,182, all offline and reproducible** |
| Enterprise features | Locked to their cloud | None | **Auth / observability / evaluation — detachable and portable** |
| What you learn | Its API | ReAct concepts | **The mental model for designing agent systems** |

**Core idea: every mechanism is an independent module with a clear Protocol boundary. Whichever piece your project is missing, take that piece — no need to pull in the whole framework.**

---

## What it can do in 30 seconds

**Minimal and runnable — zero files, zero shortcuts:**

```python
import asyncio
from prodagent import Agent, ExecutionMode, tool

@tool(name="search", readonly=True)
async def search(query: str) -> str:
    return f"results for: {query}"

agent = Agent("demo", system_prompt="Find answers.",
              tools=[search], mode=ExecutionMode.REACTIVE)

asyncio.run(agent.chat("What's the weather in Paris?"))
```

**Full production armor in one step (durable recovery + span tracing + HITL approval on HIGH tools + authorization policies + LLM cache + context compression):**

```python
from prodagent import Agent, AgentConfig
from prodagent.base.config import production

agent = Agent("demo", tools=[search],
              config=AgentConfig(name="demo", framework=production()))
```

**The full chain a single `chat()` call goes through internally:**

```
Agent.chat()
  → RunLoop (runtime entry point)
    → ReactiveLoop / PlanExecutor / Workflow (three execution modes)
      → Step (think → decide → execute atom)
        → Budget check (turns/seconds/tokens/cost, four axes)
        → Loop detection (fingerprint window)
        → Context assembly (memory recall + five-level compression)
        → LLM call (hard timeout + streaming + cache boundary)
        → Tool dispatch (readonly parallel / write serial)
          → Authorization check (RBAC + operation-level authz)
          → HITL approval gate (HIGH tools suspend for a human)
          → Tool execution + result write-back
        → Checkpoint to disk (optimistic version control)
      → Multi-agent collaboration (spawn/peer/ensemble/board/queue)
        → Crossing messaging plane (dedupe → contract → security → audit → dead letter)
  → Return result
```

---

## Key numbers

| Metric | Value | Notes |
|------|-----|------|
| Lines of code | **25,000** | The whole codebase, small enough to read end to end |
| Packages | **14** | Ordered for learning, one responsibility per package |
| Offline tests | **1,182** | Zero API keys, zero network, zero flakiness — FakeLLM reproduces precisely |
| Core dependencies | **4** | anyio / httpx / pydantic / typing-extensions |
| Protocol ports | **14** | Protocol abstractions, swappable backends (file/memory/postgres/redis/neo4j) |
| Execution modes | **3** | REACTIVE / PLAN_FIRST / Workflow |
| Collaboration primitives | **5** | Delegation / relay / voting / blackboard / work queue |
| Python versions | 3.11 – 3.14 | Full CI matrix coverage |

---

## Install

```bash
pip install prodagent
# Just 4 core dependencies, add the rest as needed:
pip install "prodagent[openai,anthropic]"    # model providers
pip install "prodagent[playground]"           # visual playground
pip install "prodagent[postgres,redis,neo4j]" # production backends
```

Pick one of three model configurations:
- `USE_FAKE_LLM=1` — fully offline, for learning and testing
- `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` — any OpenAI-compatible endpoint (DeepSeek/Qwen/Moonshot…)
- `ANTHROPIC_API_KEY` — native Anthropic

---

## Next steps

- **[Full docs →](https://limenagent.github.io/prodagent/)** — learning path · seven stations through a single call's lifecycle · deep dives into production problem domains · 9 examples
- **[5-minute quickstart →](https://limenagent.github.io/prodagent/start/)** — get your first agent running
- **[Design decisions →](https://limenagent.github.io/prodagent/decisions/)** — the "why not the other way" behind every key decision
- Companion framework to the GeekTime column [《生产级 Agent 排雷实战》](http://gk.link/a/12L6Q)

---
