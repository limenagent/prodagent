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

import asyncio
from typing import Any

from src.kernel import (
    FnBody,
    Goto,
    Node,
    Outcome,
    Plan,
    append,
)
from src.runtime.tools import DelegationSuspendedError, HardToolError


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


def opening(task: str, history: list | None = None) -> dict:
    """Build the ReAct opening state: the task becomes the first user message,
    after any prior dialogue, so the very first think already sees it.

    This is the single place that knows the conversation lives on the
    ``messages`` channel. It is a plain function called by whoever starts a Run
    (Agent.run, a delegation, a Workflow Agent node) — not a method on the Plan,
    and its result is folded and logged like any STATE_DELTA, so replay needs no
    matching special case.
    """
    messages = (
        [*history, {"role": "user", "content": task}]
        if history
        else [{"role": "user", "content": task}]
    )
    return {"messages": messages}


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
            # The messages channel is JSON-native end to end: the call list is
            # encoded as plain dicts at the source, so one shape travels shared
            # state, the event log, and the wire. The log is the only durable
            # thing — a live-object format here would fork the two truths.
            calls = [
                {"name": tc.name, "arguments": tc.arguments, "id": tc.call_id}
                for tc in reply.tool_calls
            ]
            return Outcome(
                state_delta={"messages": [{"role": "assistant", "tool_calls": calls}]},
                control=Goto("tools"),
            )
        # "content", not a bespoke "text" — the messages channel stays one
        # OpenAI-shaped vocabulary end to end (adapters and UI read it as-is).
        return Outcome(state_delta={"messages": [{"role": "assistant", "content": reply.text}]})

    async def run_tools(_input, ctx):
        # The calls the model just asked for live in the last assistant message.
        # Same-turn calls are the model's own little wave: they start together,
        # settle together, and the results land in the order they were asked.
        calls = _last_assistant(ctx.shared).get("tool_calls", [])
        results = await asyncio.gather(
            *(ctx.call_tool(c["name"], c.get("arguments") or {}) for c in calls),
            return_exceptions=True,
        )
        # Cancellation is never a "result": it propagates unchanged (the law).
        for r in results:
            if isinstance(r, asyncio.CancelledError):
                raise r
        # Same discipline as the wave barrier one level up: every call settles,
        # THEN a hard failure fails the turn — and failure wins over a sibling
        # delegation's suspension, mirroring the fail-wins wave law.
        for r in results:
            if isinstance(r, BaseException) and not isinstance(r, DelegationSuspendedError):
                raise r
        # A delegated child that parked lifts its suspension to this whole
        # flow: the turn settles, then the flow parks carrying the child's
        # run_id and question (two-step resume: resume the child, then this Run
        # with its output — the re-run turn short-circuits, no re-spawn).
        suspended = [r for r in results if isinstance(r, DelegationSuspendedError)]
        if suspended:
            # Refuse what cannot be resumed honestly: with several delegation
            # calls in one turn, a completed sibling's output is not folded at
            # park time and the single resume value cannot route back — that
            # needs a persisted call cursor, out of the teaching kernel.
            # Plain-tool siblings re-run honestly at-least-once, so they pass.
            delegations = sum(1 for c in calls if tools.is_delegation(c["name"]))
            if len(suspended) > 1 or delegations > 1:
                raise HardToolError(
                    "a turn mixing several delegation calls with a suspension "
                    "needs a persisted call cursor to route answers"
                )
            s = suspended[0]
            return Outcome.park(
                "delegation",
                payload={"child_run_id": s.child_run_id, "task": s.task},
                question=s.question,
            )
        outputs = []
        for c, result in zip(calls, results, strict=True):
            # A tool failure is fed back as a tool message, not raised into the
            # graph: the model sees it and can correct itself on the next think.
            content = result.output if result.ok else f"[tool error] {result.error}"
            # tool_call_id pairs this result with the model's own call id
            # (the provider protocol id), never with the tool name.
            outputs.append(
                {
                    "role": "tool",
                    "name": c["name"],
                    "tool_call_id": c.get("id", ""),
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
            FnBody(lambda x, ctx: Outcome.ok(_last_assistant(ctx.shared).get("content"))),
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
