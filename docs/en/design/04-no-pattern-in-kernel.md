# Design note 04: why there is no ReAct in the kernel

## A common approach that keeps swelling

Many frameworks bake ReAct (the "think a step, do a step, think again" loop)
straight into their core Agent class: a built-in while loop calls the model,
parses tool calls, and calls the model again. It's handy at first, but the moment
you want plan-then-execute, a fixed pipeline, or some custom collaboration, you
can only add mode switches and special branches to the core class. **Every new
orchestration makes the kernel fatter**, and the modes tangle so none can be
removed.

## Our choice: the kernel gives primitives, patterns are assembled

This kernel goes the other way: it has no idea what "ReAct" is. It gives a few
neutral primitives — nodes (Node), edges (Edge), two control commands (`Goto`
for back-edges/jumps, `Send` for dynamic fan-out), and "a node can hold any
executable".

So where is ReAct? It's reduced to a tiny graph: a "think" node calls the model,
an "act" node calls tools, and a **back-edge** goes from act back to think,
heading to the end when the model wants no more tools. ReAct is not a kernel
class, then, but a **recipe** assembled from primitives, living in the `runtime/`
layer:

```text
think (call model) ──wants tools──▶ act (call tool) ──back-edge Goto──▶ think
   │                                                                   (the think⇄act loop)
   └──────────────────no more tools──────────────────▶ final (finish)
```

Likewise, "plan then execute" is "a planner node + dynamically fanned-out worker
nodes + a synthesis node", and a fixed pipeline is just a few hard-wired sequential
edges. **None is a new mechanism; each is a different arrangement of the same
primitives.**

## What this buys you

- **An extremely stable kernel**: invent a hundred more collaboration styles and
  the kernel doesn't change a line — only the recipes above it.
- **Endless patterns, all bypassable**: when an official recipe doesn't fit, you
  bypass it entirely and compose your own from primitives, never blocked by the
  framework's assumptions.
- **Less cognitive load, not more**: you don't memorize "which modes this
  framework supports and how to configure each"; you understand a few primitives
  and the rest is combination.

## The cost, and how we cover it

Honestly, raw primitives aren't beginner-friendly — having everyone hand-assemble
ReAct is unrealistic. So above the primitives the project offers two layers of
"sugar": **officially maintained** common recipes in `runtime/` (ReAct,
plan-first, multi-agent), and the more ergonomic facade API (`Agent`,
`Workflow`). Ninety percent of cases work out of the box; the remaining ten
percent that need deep customization can always sink to the primitives. The key:
**this sugar lives outside the kernel — replaceable, removable — never welded
into the engine.**

## In the code

- `src/kernel/body.py`: the single composable interface; a node body can be a
  function, a tool, an LLM call, or a sub-plan.
- `src/kernel/command.py`: just two commands, `Goto / Send`.
- `src/runtime/react.py`, `plan_first.py`: see how ReAct and plan-first are
  assembled from primitives.

> Hand-assembling a ReAct from a few lines of kernel code and then collecting it
> into an ergonomic facade is a particularly satisfying part of the companion
> column. The final design note pushes the same idea to multi-agent:
> [why multi-agent needs no new engine](05-multi-agent.md).
