"""Recipes — the application layer's prebuilt shapes (column 23–29).

The kernel below knows nodes, edges, channels, runs, waves — and not one
agent loop. Everything "agentic" (a think-act loop, plan-first, the
coordination patterns) is a recipe: composed above the kernel out of the
same primitives any user would use, which is the whole proof of the
column's claim that ReAct is a strategy, not a mechanism.

``loop_body`` is the loop recipe's two halves: :class:`LoopBody` (the
declarative body a node carries — kind ``loop``, wire-friendly) and the
:class:`LoopDriver` port it drives (implemented by the runtime's
AgentLoop). The kernel delivers the driver through the NodeContext's
generic ``wiring`` bag — the kernel sees "a service a body asked for",
never "an agent loop".
"""
