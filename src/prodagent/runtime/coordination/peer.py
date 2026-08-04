"""PeerPipeline — horizontal peer handoff (``.peers()``)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from prodagent.core.error_reason import ErrorReason
from prodagent.core.types import RunState, SideEffectLevel, ToolError, ToolMeta, ToolResult
from prodagent.runtime.config import merge_tools_by_name
from prodagent.runtime.coordination.fork import ParentRuntime, describe_agent
from prodagent.tooling.base import FunctionTool

if TYPE_CHECKING:
    from prodagent.core.state.run import PendingHandoff
    from prodagent.ports import CheckpointStore
    from prodagent.runtime.agent import Agent
    from prodagent.runtime.coordination.comm import SpawnAccumulator
    from prodagent.runtime.session import RunContext

logger = logging.getLogger(__name__)


class PeerPipeline:
    """The peer's output becomes the run's output."""

    def __init__(
        self,
        peers: list[Agent],
        *,
        ctx: ParentRuntime,
    ) -> None:
        self._spec_map = {a.name: a for a in peers}
        self._ctx = ctx

    def handoff(
        self,
        peer_name: str,
        task: str,
        input_refs: dict[str, str] | None = None,
    ) -> ToolResult:
        if peer_name not in self._spec_map:
            return ToolResult.from_error(
                ToolError.from_reason(
                    ErrorReason.TOOL_NOT_AVAILABLE,
                    code="unknown_peer",
                    message=(
                        f"Unknown peer {peer_name!r}. Available: {list(self._spec_map.keys())}"
                    ),
                    hint="Declare the peer via .peers([...]) on the agent.",
                ),
                tool=f"handoff_to_{peer_name}",
            )
        return ToolResult.for_handoff(
            peer=peer_name,
            task=task,
            input_refs=input_refs,
            tool=f"handoff_to_{peer_name}",
        )

    def build_tools(self) -> list[FunctionTool]:
        tools: list[FunctionTool] = []
        for peer in self._ctx.peer_specs:
            tools.append(self._build_one_tool(peer))
        return tools

    def _build_one_tool(self, peer: Agent) -> FunctionTool:
        peer_name = peer.name
        description = describe_agent(peer) or "(no description provided)"
        schema = {
            "name": f"handoff_to_{peer_name}",
            "description": (
                f"Transfer control to peer agent {peer_name!r} and end your run.\n"
                f"{peer_name}: {description}\n\n"
                "Your run terminates with COMPLETED; the peer continues with this "
                "task plus your final output as prior context. Use this when the "
                "task belongs to the peer's speciality, not as a delegation."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            f"Specific task instruction for {peer_name}. This is what "
                            "the peer will work on — be concrete."
                        ),
                    },
                    "input_refs": {
                        "type": "object",
                        "description": (
                            "References (not content) the peer resolves via its tools "
                            '— e.g. {"order_record": "orders/123"}.'
                        ),
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["task"],
            },
        }
        meta = ToolMeta(
            name=f"handoff_to_{peer_name}",
            side_effect_level=SideEffectLevel.LOW,
            is_readonly=True,
            domain="orchestration",
        )

        pipeline = self

        async def _handoff_fn(
            task: str,
            input_refs: dict[str, str] | None = None,
        ) -> ToolResult:
            return pipeline.handoff(peer_name, task, input_refs)

        return FunctionTool(
            name=f"handoff_to_{peer_name}",
            fn=_handoff_fn,
            meta=meta,
            schema=schema,
        )


def build_peer_tools_for_agent(
    peers: list[Agent],
    *,
    ctx: ParentRuntime | None = None,
) -> list[FunctionTool]:
    if not peers:
        return []
    if ctx is None:
        ctx = ParentRuntime(peer_specs=list(peers))
    if not ctx.peer_specs:
        ctx.peer_specs = list(peers)
    pipeline = PeerPipeline(peers, ctx=ctx)
    return pipeline.build_tools()


def assemble_peer_tools(
    ctx: RunContext,
    active_tools: list[Any],
    tool_schemas: list[dict[str, Any]],
    spawn_acc: SpawnAccumulator | None,
) -> None:
    """Build peer-handoff tools for ``agent.peer_agents`` and append them to ``active_tools``/``tool_schemas``."""
    agent = ctx.agent
    if not agent.config.peer_agents:
        return
    peer_ctx = ParentRuntime.from_context(
        ctx,
        peer_specs=agent.config.peer_agents,
        accumulator=spawn_acc,
    )
    peer_tools = build_peer_tools_for_agent(agent.config.peer_agents, ctx=peer_ctx)
    added = merge_tools_by_name(active_tools, peer_tools)
    tool_schemas.extend(t.schema for t in added)


async def resolve_suspended_peer_run_id(
    store: CheckpointStore | None,
    handoff: PendingHandoff | None,
) -> str | None:
    """Return the peer's run_id if the peer is SUSPENDED; None otherwise."""
    if store is None or handoff is None:
        return None
    peer_run_id = handoff.peer_run_id
    if not peer_run_id:
        return None
    try:
        peer = await store.load(peer_run_id)
    except Exception as exc:
        logger.warning("[suspended_peer] load %s failed: %s", peer_run_id, exc)
        return None
    if peer is None or peer.state is not RunState.SUSPENDED:
        return None
    return peer_run_id


async def find_suspended_peer(
    store: CheckpointStore | None,
    root_run_id: str,
) -> tuple[str, str] | None:
    if store is None:
        return None
    root = await store.load(root_run_id)
    if root is None or root.pending_handoff is None:
        return None
    handoff = root.pending_handoff
    peer_name = handoff.peer_name
    peer_run_id = await resolve_suspended_peer_run_id(store, handoff)
    if peer_run_id is None or not peer_name:
        return None
    return peer_name, peer_run_id
