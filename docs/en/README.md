# prodagent documentation

These docs do not teach an API — for that, see the [repository README](https://github.com/limenagent/prodagent)
and `examples/`. They answer a different question: **why is this kernel shaped
the way it is, what tension does each part resolve, and what else could we have
chosen?**

The pages are ordered the way the machine "grows", each building on the
previous one, so a beginner can read them in sequence. You don't need to know
LangGraph first, and you don't need to memorize jargon — every term is explained
in plain language the first time it appears.

## Suggested order

**Step 1 — get the whole picture (~15 min)**

1. [Architecture: from a six-line loop to a machine](architecture.md) — one
   figure for the six parts and why each is needed.

**Then make it stick by hand (~30 min, strongly recommended):**
[Build a minimal kernel](build-a-minimal-kernel.md) — about 80 lines using only
the standard library, nothing to install. Type out the wave loop once yourself,
and the design notes below read like a recap instead of new abstractions.

**Step 2 — the five key design trade-offs (5–8 min each)**

2. [Why separate the blueprint from an execution](design/01-plan-and-run.md)
3. [Why state is folded from events](design/02-state-from-events.md)
4. [The wave as a consistency boundary](design/03-waves.md)
5. [Why there is no ReAct in the kernel](design/04-no-pattern-in-kernel.md)
6. [Why multi-agent needs no new engine](design/05-multi-agent.md)

**Step 3 — cross-reference and Q&A**

7. [Concept map vs LangGraph / ADK / CrewAI](comparison.md) — fastest path if
   you already use another framework.
8. [Example guide](examples.md) — what each example demonstrates and what to watch.
9. [FAQ](faq.md)
10. [Glossary](glossary.md)

## Three kinds of reader, three routes

- **New to agent frameworks**: follow the order above strictly; don't chase the
  code at first, focus on *why each part is needed*.
- **You know LangGraph / ADK and want the internals**: read the architecture
  overview, jump to the concept map, then read the design note closest to what
  you already know.
- **Reading source**: pair the README's reading order with these notes — when
  you open a file, come back here for the *why*.

## What these docs deliberately don't cover

To stay small enough to finish, these pages cover mechanism and trade-offs, not
line-by-line implementation, and not the algorithms of replaceable strategies
such as compression or retrieval. Those are built step by step in the companion
GeekTime column *Designing and Building a Production-Grade Agent Framework*,
starting from a six-line loop. These docs give you the **map and the scenery**;
the column is the **guided tour**.

中文读者请从 [docs/zh/README.md](../zh/README.md) 开始。
