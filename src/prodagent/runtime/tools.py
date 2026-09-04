"""Collaboration tool doors — spawn and handoff as tools the model can call.

Governance lives in the application; these doors only expose the roster.
The assemblers plug into the driver's ``tool_assemblers`` seam (each gets
``(ctx, active_tools, tool_schemas, acc)`` and returns the acc unchanged —
governance state, if an application ever wants one, rides the acc).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from prodagent.kernel.types import ToolResult
from prodagent.runtime.config import DEFAULT_TIMEOUT_S

if TYPE_CHECKING:
    from prodagent.runtime.agent import Agent

__all__ = [
    "assemble_peer_tools",
    "assemble_spawn_tools",
    "hop_tool_assemblers",
]


def hop_tool_assemblers() -> list[Any]:
    """Collaboration capabilities that contribute hop tools (spawn/peer).

    The driver attaches these to ``RunContext.tool_assemblers``; the driver
    consumes them blind."""
    from prodagent.runtime.tools import assemble_peer_tools, assemble_spawn_tools

    return [assemble_spawn_tools, assemble_peer_tools]


def _tool(name: str, fn: Any, description: str, *, timeout_seconds: float = 10.0) -> Any:
    """A FunctionTool via the standard inference path (annotations → schema)."""
    from prodagent.kernel.types import SideEffectLevel, ToolMeta
    from prodagent.tooling.base import FunctionTool
    from prodagent.tooling.decorator import _infer_schema

    meta = ToolMeta(
        name=name,
        side_effect_level=SideEffectLevel.LOW,
        is_readonly=True,
        timeout_seconds=timeout_seconds,
    )
    return FunctionTool(name=name, fn=fn, meta=meta, schema=_infer_schema(fn, name, description))


def assemble_spawn_tools(
    ctx: Any,
    active_tools: list[Any],
    tool_schemas: list[dict[str, Any]],
    spawn_acc: Any = None,
) -> Any:
    """One ``spawn_agent`` tool exposing the roster — the call mechanism's
    tool-shaped door. The model picks a name and a task; governance is the
    application's to compose. ``spawn_acc`` is passed through (governance
    retired with the message plane); the 4-arg signature is the assembler
    seam the driver calls."""
    agent = ctx.agent
    roster = [a.name for a in agent.child_agents]
    if not roster:
        return spawn_acc

    async def _spawn_agent(name: str, task: str) -> Any:
        child = next((a for a in agent.child_agents if a.name == name), None)
        if child is None:
            return {
                "error": "unknown_agent",
                "reason": "tool_not_available",
                "message": f"Unknown agent {name!r}. Available: {roster}",
            }
        from prodagent.kernel.types import ToolResult
        from prodagent.runtime.delegate import activate_child

        result = await activate_child(
            ctx,
            child,
            task,
            parent_run_id=ctx.run_id,
            depth=ctx.depth + 1,
        )
        if result.state == "suspended":
            # The child's park must become THIS run's park: a suspended
            # ToolResult is the one shape the dispatcher answers with a
            # park on the spawn call itself (staged for replay), so the
            # turn ends SUSPENDED carrying the child's request id — that
            # is what surfaces the approval and what resume re-drives. A
            # plain dict here would read as ordinary data and the run
            # would complete past a pending approval.
            return ToolResult.suspended(
                reason=f"child {name!r} awaiting approval",
                tool="spawn_agent",
                approval_request_id=result.approval_request_id,
            )
        return asdict(result)

    agent_lines = "\n".join(
        f"  - {a.name}: {a.config.description or a.name}" for a in agent.child_agents
    )
    description = (
        "Delegate a sub-task to a specialised sub-agent and return its result.\n"
        f"Available sub-agents:\n{agent_lines}"
    )
    tool = _tool("spawn_agent", _spawn_agent, description, timeout_seconds=DEFAULT_TIMEOUT_S)
    active_tools.append(tool)
    tool_schemas.append(tool.schema)
    return spawn_acc


def assemble_peer_tools(
    ctx: Any,
    active_tools: list[Any],
    tool_schemas: list[dict[str, Any]],
    spawn_acc: Any = None,
) -> Any:
    """One ``handoff_to_<peer>`` tool per peer — transfer's tool door.
    ``spawn_acc`` is passed through (the 4-arg assembler seam)."""
    agent = ctx.agent
    for peer in agent.config.peers or []:
        _add_handoff_tool(agent, peer.name, active_tools, tool_schemas)
    return spawn_acc


def _add_handoff_tool(
    agent: Agent, peer_name: str, active_tools: list[Any], tool_schemas: list[dict[str, Any]]
) -> None:
    async def _handoff(task: str) -> ToolResult:
        return ToolResult.for_handoff(peer=peer_name, task=task, tool=f"handoff_to_{peer_name}")

    tool = _tool(
        f"handoff_to_{peer_name}",
        _handoff,
        f"Hand the conversation to peer {peer_name!r}; it continues the chain.",
    )
    active_tools.append(tool)
    tool_schemas.append(tool.schema)
