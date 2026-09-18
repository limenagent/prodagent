"""react — recipe one: the classic ReAct agent, assembled from kernel primitives.

There is no ReAct in the kernel. Here it is assembled from three nodes, two
conditional edges, and one back-edge:

    think ──last assistant message carries tool calls?──▶ tools ──Goto back-edge──▶ think
      │
      └──last assistant message is plain text?──▶ final

It keeps a single state channel: ``messages``, the append-only chat history an
OpenAI-style API expects. Nothing else is stored — whether to call tools or to
finish is *derived* from the last assistant message, so there is no second copy
of the pending calls or the answer to keep in sync.

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
)


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content", ""))
    return ""


def _last_assistant(state: dict) -> dict:
    """The last assistant message, or {} when the history ends in a user/tool
    message (or is empty). Routing reads only this: the next step is a pure
    function of where the conversation has got to."""
    messages = state.get("messages") or []
    if messages and messages[-1].get("role") == "assistant":
        return messages[-1]
    return {}


def _wants_tools(state: dict) -> bool:
    """think→tools is live when the model just requested one or more tools."""
    return bool(_last_assistant(state).get("tool_calls"))


def _has_answer(state: dict) -> bool:
    """think→final is live when the last assistant message is plain text."""
    last = _last_assistant(state)
    return bool(last) and not last.get("tool_calls")


def build_react_plan(
    tools: Any, *, name: str = "", system: str = "", context: Any = None, memory: Any = None
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
        # Write exactly one fact: the assistant message. Where to go next is
        # derived from its shape by the conditional edges; the Goto below is what
        # re-arms tools on later rounds (a static edge never re-enters a completed
        # node). state_delta carries the message, Goto moves control.
        if reply.tool_calls:
            return Outcome(
                state_delta={"messages": [{"role": "assistant", "tool_calls": reply.tool_calls}]},
                control=Goto("tools"),
            )
        return Outcome(state_delta={"messages": [{"role": "assistant", "text": reply.text}]})

    async def run_tools(_input, ctx):
        # The calls the model just asked for live in the last assistant message;
        # run each and append its result, then Goto think so it can observe.
        outputs = []
        for call in _last_assistant(ctx.shared).get("tool_calls", []):
            result = await ctx.call_tool(call.name, call.arguments)
            # A tool failure is fed back as a tool message, not raised into the
            # graph: the model sees it and can correct itself on the next think.
            content = result.output if result.ok else f"[tool error] {result.error}"
            # tool_call_id pairs this result with the model's own call id
            # (the provider protocol id), never with the tool name.
            outputs.append(
                {
                    "role": "tool",
                    "name": call.name,
                    "tool_call_id": call.call_id,
                    "content": content,
                }
            )
        # The ReAct "loop" is exactly this Goto back-edge to think.
        return Outcome.goto("think", messages=outputs)

    plan = Plan(name=name, channels={"messages": append()})
    plan.add(
        Node("think", FnBody(think)),
        Node("tools", FnBody(run_tools)),
        # The answer is the final assistant text, read from history rather than
        # mirrored into a separate slot.
        Node(
            "final",
            FnBody(lambda x, ctx: Outcome.ok(_last_assistant(ctx.shared).get("text"))),
            terminal=True,
        ),
    )
    # think⇄tools forms a cycle:
    # - think→tools is a conditional edge (only when the last assistant message
    #   requests tools), so a direct answer never activates the tools node;
    # - across rounds tools is already COMPLETED, and the Goto("tools") returned
    #   by think re-arms it to ready;
    # - tools Gotos back to think; only the "plain text answer" edge reaches final.
    plan.edge("think", "tools", when=_wants_tools)
    plan.edge("tools", "think")
    plan.edge("think", "final", when=_has_answer)
    plan.entry = ("think",)
    return plan


def start_react_run(plan: Plan, task: str, history: list | None = None) -> Run:
    """Create a ReAct run seeded with this turn's user message (then Scheduler.drive).

    history holds prior dialogue messages for multi-turn continuation; omit it
    for a fresh conversation. The seed is folded through the messages channel and
    logged on the first drive, so even the opening message is in the event log and
    survives replay — session state is held by the caller, the Agent stays
    stateless.
    """
    opening = (
        [*history, {"role": "user", "content": task}]
        if history
        else [{"role": "user", "content": task}]
    )
    return Run.start(plan, task=task, seed={"messages": opening})
