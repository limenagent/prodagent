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

**This repo has already threaded it all together for you.**

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

### The full chain a single `chat()` call goes through internally

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
| Lines of code | **~29k** | The whole codebase, small enough to read end to end |
| Packages | **14** | Ordered for learning, one responsibility per package |
| Offline tests | **1,300+** | Zero API keys, zero network, zero flakiness — FakeLLM reproduces precisely |
| Core dependencies | **4** | anyio / httpx / pydantic / typing-extensions |
| Protocol ports | **17 (20 protocols total)** | 17 swappable ports + 3 internal collaboration contracts; backends: file/memory/postgres/redis/neo4j |
| Execution modes | **3** | REACTIVE / PLAN_FIRST / Workflow |
| Collaboration primitives | **5** | Delegation / relay / voting / blackboard / work queue |
| Bus protocols | **3** | fire (observe) / check (veto) / collect (inject) |
| Budget axes | **4** | turns / seconds / tokens / cost |
| Compression levels | **5** | none → tool compression → history summary → topic summary → emergency truncation |
| Memory channels | **4** | rule / entity / exact / semantic |
| Python versions | 3.11 – 3.14 | Full CI matrix coverage |

---

## Ten design principles at a glance

> Full treatment in [Design Philosophy](docs/design-philosophy.md).

1. **Bare core by default, production in one line** — `Agent()` starts with zero files; `production()` restores the full armor in one line. Not "everything off, turn it on yourself", but "sensible default, one-line upgrade".
2. **A pure kernel** — `kernel/` depends on no capability package. Loop, budget, state, and bus are pure logic: independently testable and replaceable.
3. **A three-protocol bus** — one seam connects every cross-cutting concern: `fire` (observe, concurrent fan-out), `check` (intercept, serial veto), `collect` (inject, concurrent gather). The loop never learns the names of approval, observability, or memory.
4. **Four budget axes back each other up** — any single axis of turns/seconds/tokens/cost can be bypassed, but all four together are very hard to defeat. Child-agent spend rolls up in real time through a shared `BudgetLedger`.
5. **Structured errors, not exceptions** — a model passing wrong arguments is normal, not exceptional. Return a `ToolError` plus a fix hint and the model corrects itself next turn; raising would break the loop and show the user "the program crashed".
6. **Five-level compression with explicit boundaries** — sacrifice in stages by token share: none → tool-result compression → history summary → topic summary → emergency truncation. The L0 system prompt and L1 state block are never compressed.
7. **Single agent is the default** — first do single-agent context management well (memory, compression, skills); only split into multiple agents when that truly isn't enough. Many "needs multi-agent" cases are really single-agent context management done poorly.
8. **One messaging plane for every topology** — all five collaboration primitives talk through the `Crossing` pipeline: dedupe → contract → security → audit → dead letter. Solving "no loss, no duplicate, no reordering" once is more reliable than writing it five times.
9. **Optimistic-concurrency recovery** — checkpoint + version control means resume after `kill -9` never re-executes. No distributed lock is needed because a Run usually has a single executor and conflicts are extremely unlikely.
10. **Fully offline and reproducible** — 1,300+ tests with zero API keys and zero network. FakeLLM controls each turn's output precisely for deterministic scenarios. The deepest way to understand a mechanism is to debug it, not memorize its conclusions.

---

## The beauty of the architecture: five recurring motifs

What makes a codebase "beautiful enough to bookmark" isn't a long feature list — it's that **the same design wisdom recurs in different places, each time exactly right**. prodagent has five such motifs; understand them and you hold the keys to the whole framework:

| Motif | In one line | Where to see it |
|------|--------|-----------|
| **Single composition root** | "Which parts to use" is decided in exactly one place — a readable checklist — so test and production can never silently diverge | `runtime/compose.py`; see [Foundation & Assembly](docs/topics/foundation.md) |
| **Mechanism vs. cross-cutting** | The loop only moves forward; approval/observability/memory all hang on one three-protocol bus, and the loop doesn't know their names | `kernel/bus.py`; see Station ⑤ |
| **Write the isomorphic once** | Three collaboration stages share one lifecycle skeleton; the varying "what a round does" is left to subclasses — adding a new pattern becomes fill-in-the-blank | `coordination/infra/`; see Station ⑦ |
| **Translate at the boundary (anti-corruption layer)** | Foreign dialects are translated into the internal standard language at the boundary: an MCP tool is indistinguishable from a local one once it crosses in | `mcp/bridge.py`; see the [MCP deep dive](docs/topics/mcp.md) |
| **Executable contracts** | "Swap storage without changing behavior" isn't a slogan — one conformance exam that memory/file/PG/Redis must all pass with full marks | `tests/backends/conformance/`; see [Backend Adapters](docs/topics/backends.md) |

The common thread: **sink whatever is error-prone, repetitive, or relies on human memory into a single place, and pin it down with tooling or tests.** To fully feel this "aesthetic of restraint", read the three deep dives in order: [Foundation & Assembly](docs/topics/foundation.md) → [Backend Adapters](docs/topics/backends.md) → [MCP External Tools](docs/topics/mcp.md).

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

## Learning path

```
5-minute quickstart
  → Part I: the lifecycle of one call (seven stations)
    → Part II: deep dives into production problem domains
      → Hands-on example map
        → Appendix: trade-offs and glossary
```

### 🚀 First step: [5-minute quickstart](docs/start.md)

Zero files, zero shortcuts — get a minimal agent running.

### 📖 Part I: the lifecycle of one call

Seven stations walk the full chain; each maps to a package in the source:

| # | Topic | What problem it solves | Source package |
|---|-------|----------------------|----------------|
| ① | [Core vocabulary](docs/tour/01-core.md) | How Agent, Run, Step, Turn, Message relate | `kernel/types` |
| ② | [Ports & contracts](docs/tour/02-ports.md) | Why Protocol instead of inheritance? How the 17 ports divide the work | `ports/` |
| ③ | [Model layer](docs/tour/03-llm.md) | LLMClient port, streaming callbacks, cache boundary, pricing | `llm/` |
| ④ | [Tool system](docs/tour/04-tools.md) | `@tool` decorator, argument validation, readonly-parallel/write-serial, tool-hallucination defense | `tooling/` |
| ⑤ | [Loop kernel](docs/tour/05-loop.md) | think→decide→execute atom, loop detection, termination and recovery | `kernel/` |
| ⑥ | [Planning & DAG](docs/tour/06-plan.md) | REACTIVE vs PLAN_FIRST vs Workflow, resumable dynamic DAG | `plan/` `runtime/` |
| ⑦ | [Multi-agent collaboration](docs/tour/07-multiagent.md) | Delegation/relay/voting/blackboard/queue, one unified messaging plane | `coordination/` |

### 🔧 Part II: production problem domains

| Domain | Topic | Core mechanism |
|------|------|---------|
| Runtime stability | [Crash recovery](docs/topics/recovery.md) | checkpoint + optimistic version control; no re-execution after kill -9 |
| | [Four-axis budget](docs/topics/budget.md) | halt on the first breach of turns/seconds/tokens/cost; child spend rolls up live |
| | [HITL approval](docs/topics/approval.md) | HIGH side-effect tools suspend; a rejection triggers incremental re-planning |
| Context & memory | [Five-level compression](docs/topics/compression.md) | staged sacrifice by token share; key constraints are never compressed away |
| | [Four-channel memory](docs/topics/memory.md) | rule/entity/exact/semantic recall in parallel + conflict arbitration |
| | [Skill loop](docs/topics/skills.md) | distil successful runs into runbooks; gets steadier with use |
| Multi-agent governance | [Policy & governance](docs/topics/governance.md) | loop backstops, no-loss/no-dup/no-reorder messages, policy engine |
| Observability & evaluation | [Full-chain tracing](docs/topics/observability.md) | OpenTelemetry-compatible spans, chain-of-thought persisted |
| | [Evaluation & regression](docs/topics/evaluation.md) | offline eval + automatic scoring of live traces, calibrated LLM-as-judge |
| Foundation & adapters | [Foundation & assembly](docs/topics/foundation.md) | base craftsmanship, the single composition root, self-wiring bundles, leaf isolation |
| | [Backend adapters](docs/topics/backends.md) | five backend families, three concurrency strategies, one conformance exam |
| | [MCP external tools](docs/topics/mcp.md) | four translations in the anti-corruption layer; remote tools become first-class citizens |

### 🎯 Hands-on: [10 end-to-end examples](docs/examples.md)

Each example maps to a real production scenario and teaches a complete combination of mechanisms.

### 📚 Deep reading

- [Architecture overview](docs/architecture.md) — layering, data flow, control flow, and a deep look at key abstractions
- [Design philosophy](docs/design-philosophy.md) — 10 core principles, each with a "why" and a counter-example
- [Mental model](docs/mental-model.md) — the full lifecycle of one call, with deeper source walkthrough than the seven stations
- [Design trade-offs](docs/decisions.md) — the "why not the alternative" behind every key decision
- [Foundation & assembly](docs/topics/foundation.md) — lazy loading, atomic writes, composition root, self-wiring bundles: the craftsmanship of the whole repo
- [Backend adapters](docs/topics/backends.md) — how memory/file/Postgres/Redis/Neo4j swap seamlessly without changing behavior
- [MCP external tools](docs/topics/mcp.md) — one anti-corruption layer that puts external tools through the same dispatch/approval/circuit-breaker pipeline
- [Glossary](docs/glossary.md) — quick reference
- [API reference](docs/reference.md) — auto-generated interface docs

---

## The package directory is the learning order

```
src/prodagent/
├── base/          ← foundations: config, errors, retry, event log
├── ports/         ← 17 swappable ports (the "left" side of hexagonal architecture; 20 protocols total)
├── llm/           ← model adapters: OpenAI/Anthropic/Fake + pricing
├── tooling/       ← tool system: decorator, dispatch, registry, reliability
├── kernel/        ← kernel: loop, step, budget, event bus, state
├── runtime/       ← runtime: agent assembly, factory, parent runtime
├── plan/          ← planning: dynamic DAG, PlanExecutor, Workflow
├── coordination/  ← multi-agent: spawn/peer/ensemble/board/queue
├── cognition/     ← cognition: context compression, four-channel memory
├── hooks/         ← cross-cutting: approval, authz, observability, audit
├── skills/        ← skills: runbook distillation and recall
├── backends/      ← port implementations: file/memory/postgres/redis/neo4j
├── mcp/           ← MCP bridge: stdio/HTTP external tools
└── playground/    ← visualization (a leaf isolated by import-linter)
```

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

👉 **[Start with the 5-minute quickstart →](docs/start.md)**

Or jump straight into the [Architecture overview](docs/architecture.md) / [Design philosophy](docs/design-philosophy.md) / [Part I · the lifecycle of one call](docs/tour/index.md).

---

## License

AGPL-3.0-only — see [LICENSE](LICENSE) for details.

Contributions require signing the [CLA](CLA.md).

---

> **If you find this framework valuable, please leave a Star ⭐. Every Star is a vote that good architecture deserves to be seen.**
