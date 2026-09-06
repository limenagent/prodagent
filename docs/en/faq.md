# FAQ

## How is it related to LangGraph? Is it a replacement?

No, it's complementary. LangGraph is a full-featured production framework with a
rich ecosystem; prodagent is a **reference implementation** small enough to read
in a weekend, explaining the shared kernel of graph, state, scheduling, events,
interrupts, and multi-agent with minimal code. See the [concept map](comparison.md).
A common path: learn the principles here, then go back to LangGraph and work
productively.

## Why zero runtime dependencies? Is that real?

Yes. The kernel uses only the Python standard library: concurrency via
`asyncio`, persistence via atomic file writes, the playground via `http.server`.
This keeps you from being interrupted by third-party magic while reading the
kernel, and makes offline tests fast and stable. For a real model,
`openai_lite.py` likewise calls any OpenAI-compatible endpoint with the standard
library, no SDK.

## Do I need an API key? Does it cost anything?

To run the examples, tests, or playground: **no**. They use `ScriptedLlm`, which
plays the model from a script, with deterministic results you can rerun freely.
You only set `OPENAI_API_KEY` (optionally `OPENAI_BASE_URL`, `OPENAI_MODEL`) when
you want a real LLM.

## Can a beginner learn this? Do I need a framework first?

You don't need any agent framework beforehand, but basic Python (functions,
classes, `async/await`) helps. The route is the one in the [documentation
guide](README.md): architecture overview for the big picture, then the five
design notes, then `examples/` and `tests/`. Every term is explained in plain
words on first use; the [glossary](glossary.md) is always there.

## What does "production-grade" mean here?

Here it means **the kernel has clear execution semantics**: state is persistent,
failures recoverable, concurrent results deterministic, runs can pause for human
approval, and extension boundaries are explicit. It is not a turnkey enterprise
platform — multi-tenancy, distributed cluster scheduling, model evaluation, and
a business-governance console are out of scope. The teaching build is single-process
with file persistence; swapping storage and other ports for database
implementations is a clearly reserved extension, not something the project finishes
for you.

## Why is there no ReAct / no "execution-mode enum" in the kernel?

Because those are **strategy**, not **mechanism**. ReAct, plan-first, and
multi-agent are all assembled on top from the same small set of primitives; the
kernel offers only neutral nodes, edges, commands, and scheduling. See
[design note 04](design/04-no-pattern-in-kernel.md).

## Why is there no concept like a Turn?

The design deliberately pursues "fewest, orthogonal concepts". Anything existing
primitives can absorb doesn't get its own name. A notion like "the boundary of a
dialogue round / a batch of tool calls" is expressed naturally here by "node +
wave + state_delta", without a separate noun. Fewer concepts mean a more stable,
easier-to-learn kernel.

## Does multi-agent really need no new engine?

No. One agent calling another is essentially a node body that recursively
activates a sub-graph (a child Run) on the same scheduler. What you distinguish is
just two orthogonal axes: control flow (call returns / transfer doesn't /
blackboard shares) and state (isolated / shared). See
[design note 05](design/05-multi-agent.md).

## Something's wrong — where do I ask?

- First check the relevant design note and the same-named test under `tests/` —
  tests are the smallest, most deterministic usage examples.
- Still unclear? Open a GitHub issue with the "Learning question" template. A spot
  where a learner gets stuck counts as a bug in a teaching project.

## How does this relate to the GeekTime column "Designing and Building a Production-Grade Agent Framework"?

This repository is the column's companion code and the final landing point of its
derivations. The docs explain *what* and *why this trade-off*; the column builds
the code with you from a six-line loop, step by step, including every derivation,
the rejected alternatives, and the pitfalls. In short: the docs are the map and
the scenery, the column is the guided tour.
