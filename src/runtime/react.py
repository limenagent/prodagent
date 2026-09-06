"""react — recipe one: assemble ReAct from kernel primitives.

There is no ReAct in the kernel. Here it is assembled from three nodes, two
conditional edges, and one back-edge:

    think ──tool calls?──▶ tools ──Goto back-edge──▶ think
      │
      └──no tool calls, answer ready?──▶ final

Context compression and long-term memory are both **optionally injected
strategies**: pass them in and they take effect before "think"; leave them out
and it still runs fine — neither the kernel nor this recipe depends on their
concrete implementation.
"""

from __future__ import annotations

from typing import Any

from src.kernel import (
    FnBody,
    Goto,
    Node,
    Outcome,
    Plan,
    Run,
    append,
    last,
)


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content", ""))
    return ""


def build_react_plan(
    tools: Any, *, system: str = "", context: Any = None, memory: Any = None
) -> Plan:
    """tools is a ToolPort-compatible tool registry (see runtime.tools.ToolRegistry)."""

    async def think(_input, ctx):
        messages = list(ctx.shared["messages"])
        if context is not None:  # strategy: context-window assembly/compression
            messages = await context.assemble(messages)
        sys_text = system
        if memory is not None:  # strategy: retrieve long-term memory, then inject
            recalled = await memory.recall(_last_user_text(messages))
            if recalled:
                sys_text = (system + "\n\n" if system else "") + "Relevant memory:\n" + recalled

        async def _delta(
            piece, kind="content"
        ):  # token streaming: emit to bus live (not into event log)
            await ctx.emit("llm_delta", text=piece, kind=kind)

        reply = await ctx.llm_chat(
            messages, tools=tools.schemas(), system=sys_text or None, on_delta=_delta
        )
        if reply.tool_calls:
            # Tools requested: use Goto to explicitly make tools ready again (it
            # is re-entered repeatedly across multi-round calls).
            return Outcome(
                state_delta={
                    "messages": [{"role": "assistant", "tool_calls": reply.tool_calls}],
                    "pending": list(reply.tool_calls),
                },
                control=Goto("tools"),
            )
        return Outcome(
            state_delta={
                "messages": [{"role": "assistant", "text": reply.text}],
                "answer": reply.text,
            }
        )

    async def run_tools(_input, ctx):
        outputs = []
        for call in ctx.shared["pending"]:
            result = await ctx.call_tool(call.name, call.arguments)
            content = result.output if result.ok else f"[tool error] {result.error}"
            outputs.append({"role": "tool", "name": call.name, "content": content})
        # Clear the pending list and use Goto to make think ready again — the
        # ReAct "loop" is exactly this back-edge.
        return Outcome.goto("think", messages=outputs, pending=[])

    plan = Plan(channels={"messages": append(), "pending": last(None), "answer": last(None)})
    plan.add(
        Node("think", FnBody(think)),
        Node("tools", FnBody(run_tools)),
        Node("final", FnBody(lambda x, ctx: Outcome.ok(ctx.shared["answer"])), terminal=True),
    )
    # think⇄tools forms a cycle:
    # - think→tools is a conditional edge (only when pending is non-empty), so a
    #   direct answer never accidentally activates the tools;
    # - across multi-round calls tools is already COMPLETED, and the Goto("tools")
    #   returned by think re-arms it to ready;
    # - tools Gotos back to think; only the "answer ready" conditional edge reaches final.
    plan.edge("think", "tools", when=lambda s: bool(s.get("pending")))
    plan.edge("tools", "think")
    plan.edge("think", "final", when=lambda s: bool(s.get("answer")))
    plan.entry = ("think",)
    return plan


def start_react_run(plan: Plan, task: str, history: list | None = None) -> Run:
    """Create a ReAct run and seed it with this turn's user message (then Scheduler.drive).

    history holds prior dialogue messages: pass it for multi-turn continuation
    (the new Run thinks with the old context), omit it for a fresh conversation —
    session state is held by the caller, and the Agent itself stays stateless.
    """
    run = Run.start(plan, task=task)
    run.shared["messages"] = (
        [*history, {"role": "user", "content": task}]
        if history
        else [{"role": "user", "content": task}]
    )
    return run
