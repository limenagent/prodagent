# prodagent

> **A production-grade LLM agent framework — small enough to finish reading, complete enough to run in production, beautiful enough to bookmark.**
>
> ~29k lines · 14 packages · 1,300+ offline tests · only 4 core dependencies

[![PyPI](https://img.shields.io/pypi/v/prodagent?color=blue&logo=pypi&logoColor=white)](https://pypi.org/project/prodagent/)
[![Python](https://img.shields.io/pypi/pyversions/prodagent?logo=python&logoColor=white)](https://pypi.org/project/prodagent/)
[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/limenagent/prodagent/ci.yml?logo=github&label=CI)](https://github.com/limenagent/prodagent/actions)
[![Tests](https://img.shields.io/badge/tests-1%2C300%2B-offline-green)]()
[![Dependencies](https://img.shields.io/badge/core%20deps-4-purple)]()

[中文](README.md) · **English** · Companion framework to the GeekTime column [《生产级 Agent 排雷实战》](http://gk.link/a/12L6Q)

---

## In one sentence

**prodagent is an industrial-grade agent framework you can read end to end and build a complete mental model of in your head.**

It's neither yet another black-box SDK nor a few-dozen-line teaching toy. It sits in the middle: every mechanism — loop, budget, recovery, approval, authorization, observability, multi-agent collaboration — is small enough to understand in one sitting and complete enough to carry straight into production.

---

## You can use LangChain — but can you design an agent system?

Scan 2026's agent job postings and the tiers are clear.

- **Junior roles** ask for "proficient with LangChain/LangGraph, can write prompts, has done RAG" — three months to get there, so the supply is the most crowded.
- **Senior / architect roles** read completely differently: "build a production-grade agent system from 0 to 1", "deeply customize or build core modules in-house", "multi-agent architecture design" — two to three times the pay, and very few people can take it.

In interviews, nobody asks "how do you initialize LangChain's `AgentExecutor`" — they ask:

- How many layers of armor does a `while True` loop that calls a model need before it's production-ready?
- How do the four-axis budgets — turns / seconds / tokens / cost — all take effect at once, halting on the first breach?
- When a long task gets `kill -9` halfway, how do you resume from a checkpoint without losing state or re-executing steps?
- When a model calls a tool that doesn't exist, or passes wrong arguments, how do you prevent it?
- When should you split into multiple agents, and when is a single agent with good context management enough?
- You changed a prompt — how do you know whether it got better or worse?

These questions can't be answered by papers or API docs. The solutions are scattered across issue threads, cloud-vendor docs, and some open-source framework's source — you'd have to dig through hundreds of thousands of lines and piece it together yourself.

This repo teaches **how to design an industrial-grade agent framework from scratch**, and ships a complete reference implementation — prodagent.

| Part | Ch | Topic |
|---|---|---|
| The map | 1 | [From a 10-line demo to an industrial framework](docs/book/ch01.md) |
| Single agent | 2 | [Model layer: the brain](docs/book/ch02.md) |
| | 3 | [Loop kernel: the heartbeat](docs/book/ch03.md) |
| | 4 | [Budget: the spending gate](docs/book/ch04.md) |
| | 5 | [Tool system: the hands](docs/book/ch05.md) |
| | 6 | [Memory, compression & skills](docs/book/ch06.md) |
| | 7 | [Event log & crash recovery](docs/book/ch07.md) |
| | 8 | [Planning & DAG](docs/book/ch08.md) |
| | 9 | [Approval: the gate on irreversible actions](docs/book/ch09.md) |
| Multi-agent | 10 | [Multi-agent collaboration](docs/book/ch10.md) |
| Observe & replay | 11 | [Observability: no more black box](docs/book/ch11.md) |
| | 12 | [The replayable, rollback-able runtime](docs/book/ch12.md) (finale) |
| Appendix | — | [Knowledge index / principles / trade-offs / glossary / examples / API](docs/book/appendix.md) |

**[👉 Start from the preface — running in 5 minutes →](docs/book/ch00.md)**

---

---

## Why prodagent, not something else

Agent projects on the market sit at two extremes; prodagent is in the middle:

| | Black-box frameworks (LangChain / AutoGen) | Teaching toys (a few dozen lines of ReAct) | **prodagent** |
|---|---|---|---|
| Feature completeness | High | Low | **High** |
| Code readability | Thick abstraction layers, can't touch the core | Clear, but no production mechanisms | **~29k lines, ordered for learning** |
| Budget / recovery / approval | None, or locked to the cloud | None | **Built in, on by default** |
| Core dependencies | Dozens (transitive) | 1–2 | **4** |
| Tests | Depend on real APIs | Almost none | **1,300+, all offline and reproducible** |
| Enterprise features | Locked to their cloud | None | **Auth / observability / evaluation — detachable and portable** |
| What you learn | Its API | ReAct concepts | **The mental model for designing agent systems** |

**Core idea: every mechanism is an independent module with a clear Protocol boundary. Whichever piece your project is missing, take that piece — no need to pull in the whole framework.**

---

## What it can do in 30 seconds

### Minimal and runnable — zero files, zero shortcuts

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

### Full production armor in one step

Durable recovery + span tracing + approval on HIGH tools + authorization policies + LLM cache + context compression — switch it all on in one line:

```python
from prodagent import Agent, AgentConfig
from prodagent.base.config import production

agent = Agent("demo", tools=[search],
              config=AgentConfig(name="demo", framework=production()))
```

The full chain inside that call — from execution mode to the approval gate — lives in [chapter 1's architecture map](docs/book/ch01.md) and [chapter 3's loop kernel](docs/book/ch03.md).

## The beauty of the architecture: five recurring motifs

What makes a codebase "beautiful enough to bookmark" isn't a long feature list — it's that **the same design wisdom recurs in different places, each time exactly right**. prodagent has five such motifs; understand them and you hold the keys to the whole framework:

| Motif | In one line | Where to see it |
|------|--------|-----------|
| **Single composition root** | "Which parts to use" is decided in exactly one place — a readable checklist — so test and production can never silently diverge | `runtime/compose.py`; see the [book](docs/book/ch01.md) |
| **Mechanism vs. cross-cutting** | The loop only moves forward; approval/observability/memory all hang on one three-protocol bus, and the loop doesn't know their names | `kernel/bus.py`; see Station ⑤ |
| **Write the isomorphic once** | Three collaboration stages share one lifecycle skeleton; the varying "what a round does" is left to subclasses — adding a new pattern becomes fill-in-the-blank | `coordination/infra/`; see Station ⑦ |
| **Translate at the boundary (anti-corruption layer)** | Foreign dialects are translated into the internal standard language at the boundary: an MCP tool is indistinguishable from a local one once it crosses in | `mcp/bridge.py`; see the [book's tool chapter](docs/book/ch05.md) |
| **Executable contracts** | "Swap storage without changing behavior" isn't a slogan — one conformance exam that memory/file/PG/Redis must all pass with full marks | `tests/backends/conformance/`; see the [book's model-layer chapter](docs/book/ch02.md) |

The common thread: **sink whatever is error-prone, repetitive, or relies on human memory into a single place, and pin it down with tooling or tests.** To fully feel this "aesthetic of restraint", read the book in order: [chapter 1](docs/book/ch01.md) → [chapter 2](docs/book/ch02.md) → [chapter 5](docs/book/ch05.md).

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

## Community & contributing

**This framework grows with you.** Whether you want to learn agent-system design or use it in your own projects, you're welcome to join:

- ⭐ **Star it** — help more people discover the project
- 🐛 **Open an issue** — bugs, questions, feature requests are all welcome
- 🔧 **Send a PR** — from doc fixes to core mechanisms, every contribution counts
- 💬 **Discuss design** — have an idea about the architecture? Talk about it in Discussions
- 📝 **Write an example** — your use case is the best tutorial

See [CONTRIBUTING](CONTRIBUTING.md) for details.

### Contributor-friendly design

- **High-quality comments in every module** — comments explain the *why*, not the *what*
- **1,300+ offline tests** — after a change, run `pytest` and know within 30 seconds whether anything broke
- **Clear Protocol boundaries** — adding a backend = implementing one Protocol, without touching the core
- **import-linter enforces layering** — CI checks dependency direction automatically, so the architecture can't be scrambled by accident

---

## Roadmap

Done so far: core loop, three execution modes, five collaboration primitives, four-axis budget, five-level compression, four-channel memory, HITL approval, crash recovery, observability, the MCP bridge, and 5 backends.

Future directions: distributed runtime, streaming multi-agent, more evaluation metrics, a plugin marketplace, enterprise RBAC.

See the [Roadmap](ROADMAP.md) for details.

---

## FAQ

**Q: How is this different from LangChain / LangGraph?**
A: LangChain is a toolbox and LangGraph is a state machine. prodagent is a complete agent runtime with production mechanisms built in — budget, recovery, approval, authorization, observability. More importantly, its code is small enough to read end to end and build a complete mental model from — something a black-box framework can't give you.

**Q: Can I use just part of it?**
A: Yes. Every mechanism is an independent module with a clear Protocol boundary. Take whichever piece your project is missing. For example, if you only want the four-axis budget, use `kernel/budget.py` directly; if you only want context compression, use `cognition/context/`.

**Q: Is it production-ready?**
A: Yes. `production()` turns on the full armor in one line: durable recovery, span tracing, HITL approval on HIGH tools, authorization policies, LLM cache, and context compression. Backends support file (single host) / postgres (multi-replica) / redis (cache & locks) / neo4j (graph).

More questions in the [FAQ](FAQ.md).

---

## Next steps

👉 **[Start with the 5-minute quickstart →](docs/book/ch00.md)**

Or start straight from [chapter 1: the architecture map](docs/book/ch01.md).

---

## License

AGPL-3.0-only — see [LICENSE](LICENSE) for details.

Contributions require signing the [CLA](CLA.md).

---

> **If you find this framework valuable, please leave a Star ⭐. Every Star is a vote that good architecture deserves to be seen.**
