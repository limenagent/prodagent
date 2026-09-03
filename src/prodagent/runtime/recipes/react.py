"""react — the think-act loop as two nodes and a back edge (column 23).

The kernel knows nothing about ReAct. What a "ReAct agent" *is*, reduced to
the graph vocabulary, is exactly this: an ``agent`` node (one model call
over the message channel), a ``tools`` node (execute the batch the model
asked for), one conditional edge (``agent → tools`` only when the model
asked), and one back edge (``tools → agent``). The model's only power is
voting on the conditional edge; the loop's skeleton is edges, decided by
structure, never by the model.

This is the *reference* L1 shape — the LoopBody recipe is the same loop
wrapped as one node, this one unfolds it. Both run on the one Scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from prodagent.kernel.body import NodeContext, Outcome
from prodagent.kernel.channels import append, last
from prodagent.kernel.graph import Node, Plan, compile_planned

if TYPE_CHECKING:
    from prodagent.kernel.types import ToolCall

__all__ = [
    "LLM_CLIENT_KEY",
    "DISPATCHER_KEY",
    "ReActAgent",
    "ThinkBody",
    "ToolsBody",
    "build_react_plan",
]

LLM_CLIENT_KEY = "llm_client"
"""Wiring-bag key for the raw LLM client (``complete`` returns the full
response — tool_calls and all), which a fixed-prompt invoker can't expose."""
DISPATCHER_KEY = "dispatcher"
"""Wiring-bag key for the tool dispatcher — the same governed five-gate
throat every tool path shares."""

_MESSAGES = "messages"
_PENDING = "pending_tool_calls"


@dataclass(frozen=True)
class ThinkBody:
    """The agent node: one model call over the message channel.

    Its answer is either a final text (the node's value — the run's output)
    or a batch of tool calls, parked on ``pending_tool_calls`` for the tools
    node — which is what the conditional edge reads. It never decides
    control flow; it only writes state the edges already know how to read."""

    system: str = ""
    tools: tuple[Any, ...] = ()
    kind = "think"
    readonly = True
    drives_run = True
    """The think node writes the run's transcript (the message channel) and
    its final answer IS the run's output — unwrapped, no tool fragment."""

    @property
    def target(self) -> str:
        return "think"

    async def run(self, input: ToolCall, ctx: NodeContext) -> Outcome:
        llm = ctx.wiring.get(LLM_CLIENT_KEY)
        if llm is None:
            raise RuntimeError("think node: no LLM client on the wiring bag")
        messages = list(ctx.shared.get(_MESSAGES, []))
        response = await llm.complete(
            messages,
            system=self.system,
            tools=list(self.tools) or None,
            config=getattr(llm, "default_config", None),
        )
        assistant: dict[str, Any] = {"role": "assistant", "content": response.content or ""}
        delta: dict[str, Any] = {_MESSAGES: [assistant]}
        if response.tool_calls:
            assistant["tool_calls"] = [c.to_dict() for c in response.tool_calls]
            delta[_PENDING] = list(response.tool_calls)
            return Outcome(state_delta=delta)
        # final answer: the node's value IS the run's output
        return Outcome(value=response.content, state_delta=delta)


@dataclass(frozen=True)
class ToolsBody:
    """The tools node: execute the parked batch, write the results back."""

    kind = "tools"
    readonly = False

    @property
    def target(self) -> str:
        return "tools"

    async def run(self, input: ToolCall, ctx: NodeContext) -> Outcome:
        dispatcher = ctx.wiring.get(DISPATCHER_KEY)
        if dispatcher is None:
            raise RuntimeError("tools node: no dispatcher on the wiring bag")
        calls = list(ctx.shared.get(_PENDING, []))
        tool_messages: list[dict[str, Any]] = []
        for call in calls:
            result = await dispatcher.dispatch(call, run_id=ctx.run_id)
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "content": (
                        result.value if result.value is not None else (result.reason or "")
                    ),
                }
            )
        return Outcome(state_delta={_MESSAGES: tool_messages, _PENDING: []})


def build_react_plan(*, system: str = "", tools: tuple[Any, ...] = ()) -> Plan:
    """The two-node graph: think → tools → think, with the exit riding the
    conditional edge (no pending calls ⇒ the tools edge waives ⇒ the loop
    ends). ``messages`` is the append channel; ``pending_tool_calls`` is the
    last-writer control the conditional edge reads."""
    plan = compile_planned(
        [
            Node(node_id="think", body=ThinkBody(system=system, tools=tools), is_terminal=True),
            Node(node_id="tools", body=ToolsBody()),
        ]
    )
    plan.edge("think", "tools", when=lambda s: bool(s.get(_PENDING)), back=False)
    plan.edge("tools", "think", back=True)  # the loop's back edge
    plan.declare_channels(
        {
            _MESSAGES: append([]),
            _PENDING: last([]),
        }
    )
    return plan


@dataclass
class ReActAgent:
    """The L1 prebuilt (column 23): ``ReActAgent(tools, model).run(task)``.

    A thin wrapper over :func:`build_react_plan` — the plan is the agent,
    there is no class behind the curtain."""

    system: str = ""
    tools: tuple[Any, ...] = ()
    model: Any = None
    dispatcher: Any = None

    def build(self) -> Plan:
        return build_react_plan(system=self.system, tools=self.tools)

    async def run(self, task: str) -> Any:
        from prodagent.kernel.scheduler import Scheduler

        scheduler = Scheduler(
            initial_plan=self.build(),
            wiring={LLM_CLIENT_KEY: self.model, DISPATCHER_KEY: self.dispatcher},
        )
        terminal = None
        async for ev in scheduler.stream(task):
            terminal = ev
        return terminal
