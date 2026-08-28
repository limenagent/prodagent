"""Peer — horizontal handoff (``peers=``), the whole chain in one place.

Three parts, one file:

- **Tools** (``Peer``): the ``handoff_to_<peer>`` schemas the model calls.
- **Relay** (``PeerRelay``): decides, for a run that parked a handoff, whether
  and where the chain continues — settle-at-handoff budget commit, the relay
  pipeline (dedupe spans the whole chain), checkpoint persistence — and
  returns a pure-data :class:`~prodagent.ports.runner.HandoffActivation`.
- The chain driver (``runtime/runner.py``) reaches the relay through the
  compose seam (``compose.peer_relay``) and interprets that descriptor —
  peer lookup, fork, next hop. Coordination never constructs runtime objects.

Peer is a *delegation strategy*, not a multi-round staged topology: contrast
with ensemble/blackboard/work_queue, which run their own round loop over a
shared store."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from prodagent.base.errors import BudgetExceeded, ErrorReason
from prodagent.coordination.messaging.envelope import Crossing, CrossingKind, Direction
from prodagent.coordination.messaging.packet import HandoffPacket
from prodagent.coordination.messaging.transport import (
    PipelineTransport,
    TransportSpec,
    build_transport,
)
from prodagent.kernel.budget import hop_own_share
from prodagent.kernel.bus import HookEvent, save_and_fire_checkpoint
from prodagent.kernel.state import AgentRun, child_run_id
from prodagent.kernel.types import RunState, SideEffectLevel, ToolError, ToolMeta, ToolResult
from prodagent.ports.runner import HandoffActivation
from prodagent.tooling.base import FunctionTool
from prodagent.tooling.merge import attach_tools

if TYPE_CHECKING:
    from prodagent.kernel.budget import SpawnAccumulator
    from prodagent.kernel.state import PendingHandoff
    from prodagent.ports import CheckpointStore, EventLog
    from prodagent.ports.budget_ledger import BudgetLedgerPort
    from prodagent.runtime.agent import Agent

logger = logging.getLogger(__name__)


class Peer:
    """Backs ``peers=`` (horizontal hand-off): ``handoff_to_<peer>`` ends the
    current run with ``COMPLETED`` and hands control to the peer, which
    continues with this task plus the caller's final output. Contrast with
    :class:`~prodagent.coordination.spawn.Spawn` (``agents=``, vertical
    delegation): parent keeps running, gets a result back."""

    def __init__(self, peers: list[Agent]) -> None:
        self._spec_map = {a.name: a for a in peers}

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
                    hint="Declare the peer via peers=[...] on the agent.",
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
        return [self._build_one_tool(peer) for peer in self._spec_map.values()]

    def _build_one_tool(self, peer: Agent) -> FunctionTool:
        peer_name = peer.name
        description = peer.spec().describe() or "(no description provided)"
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


def build_peer_tools_for_agent(peers: list[Agent]) -> list[FunctionTool]:
    if not peers:
        return []
    return Peer(peers).build_tools()


def assemble_peer_tools(
    ctx: Any,
    active_tools: list[Any],
    tool_schemas: list[dict[str, Any]],
    spawn_acc: SpawnAccumulator | None,
) -> SpawnAccumulator | None:
    """Build peer-handoff tools for ``agent.config.peers``, appended to
    ``active_tools``/``tool_schemas``. Returns ``spawn_acc`` unchanged — peer
    handoff doesn't create its own accumulator, but passing it through keeps
    the call shape symmetric with ``assemble_spawn_tools``.

    ``ctx`` is the hop's RunContext — runtime vocabulary, read structurally."""
    agent = ctx.agent
    if not agent.config.peers:
        return spawn_acc
    peer_tools = build_peer_tools_for_agent(agent.config.peers)
    attach_tools(active_tools, tool_schemas, peer_tools)
    return spawn_acc


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


# ── PeerRelay — the hop-by-hop handoff driver ─────────────────────────────────


class PeerRelay:
    """Owns the relay pipeline (dedupe spans the whole chain) and decides,
    for a run that parked a handoff, whether and where the chain continues."""

    def __init__(self, root_run_id: str) -> None:
        self._root_run_id = root_run_id
        self._transport: PipelineTransport | None = None

    async def next_hop(
        self,
        agent: Agent,
        run: AgentRun,
        *,
        run_id: str,
        depth: int,
        checkpoint: CheckpointStore | None,
        event_log: EventLog | None,
        spawn_acc: SpawnAccumulator | None = None,
        ledger: BudgetLedgerPort | None = None,
    ) -> HandoffActivation | None:
        """The next hop as pure data, or ``None`` when the chain stops here.

        ``agent`` is the hop that just finished (peer lookup, constraints,
        framework config); ``run_id``/``depth``/``checkpoint``/``event_log``
        are that hop's context. Interpreting the returned descriptor — peer
        fork, next hop context — is the chain driver's business."""
        if run.pending_handoff is None:
            return None
        handoff = run.pending_handoff
        fw = agent.framework_config
        if depth >= fw.orchestration.max_peer_chain:
            # Peers bouncing the task back and forth is a livelock, not
            # progress — the chain cap turns it into a bounded settle.
            return None

        peer_name = handoff.peer_name
        peer_spec = agent.peer_named(peer_name)
        if peer_spec is None:
            logger.error(
                "[orchestrator] peer %r not found on agent %r — chain stops",
                peer_name,
                agent.name,
            )
            return None

        if ledger is not None:
            # Commit only this hop's OWN share: children already committed
            # live, and the fold folded their totals into run.metrics —
            # committing the post-fold numbers would count them twice.
            own_turns, own_tokens, own_cost = hop_own_share(run, spawn_acc)
            await ledger.commit(
                member=agent.name,
                turns=own_turns,
                tokens=own_tokens,
                cost_usd=own_cost,
            )
            try:
                await ledger.check(member=peer_name)
            except BudgetExceeded as exc:
                logger.warning(
                    "[orchestrator] peer chain budget exhausted before handoff %s → %s: %s",
                    agent.name,
                    peer_name,
                    exc,
                )
                return None

        prior_output = run.final_output or ""
        packet = HandoffPacket(
            task_description=handoff.task,
            constraints=list(agent.constraints),
            available_tools=[t.name for t in peer_spec.inline_tools],
            input_refs=handoff.input_refs or {},
            prior_output=prior_output,
        )
        if not handoff.message_id:
            handoff.message_id = str(uuid.uuid4())  # checkpoint written pre-migration
        peer_run_id = child_run_id(self._root_run_id, peer_name)
        handoff.peer_run_id = peer_run_id  # persist on the run before save below

        delivery = await self._transport_for(agent).send(
            Crossing.mint(
                direction=Direction.DOWNSTREAM,
                kind=CrossingKind.HANDOFF,
                from_agent=agent.name,
                to=peer_name,
                payload=packet,
                trace_id=self._root_run_id,
                message_id=handoff.message_id,
                depth=depth + 1,
                parent_run_id=run_id,
                child_run_id=peer_run_id,
            )
        )
        if delivery.status != "delivered":
            # A duplicate relay (checkpointed handoff replayed in-process) or a
            # gate veto — the chain stops here and the current run settles.
            logger.warning(
                "[orchestrator] handoff %s → %s not delivered (%s): %s",
                agent.name,
                peer_name,
                delivery.status,
                delivery.reason,
            )
            return None

        if checkpoint is not None:
            await save_and_fire_checkpoint(checkpoint, run, agent.hooks)

        return HandoffActivation(
            peer_name=peer_name,
            task=packet.to_task_prompt(),
            run_id=peer_run_id,
            parent_run_id=run_id,
            depth=depth + 1,
        )

    def _transport_for(self, agent: Agent) -> PipelineTransport:
        """The relay's DOWNSTREAM transport, built on first handoff.

        Dedupe is shared across the whole chain (one handler per relay);
        hooks are read at first relay, when executor preparation has attached
        them. Built through the shared transport factory like every other
        boundary; the PEER_HANDOFF audit event fires from the pipeline's last
        slot — only for crossings that were actually delivered.
        """
        if self._transport is None:
            fw = agent.framework_config
            orch = fw.orchestration if fw is not None else None
            ttl = orch.handoff_idempotency_ttl_s if orch is not None else 600.0
            self._transport = build_transport(
                TransportSpec(
                    direction=Direction.DOWNSTREAM,
                    dedupe_ttl_s=ttl,
                    hooks=agent.hooks,
                    audit_event=self._audit_event,
                )
            )
        return self._transport

    @staticmethod
    def _audit_event(
        crossing: Crossing[Any],
    ) -> tuple[HookEvent, dict[str, Any]] | None:
        packet = crossing.payload
        task = getattr(packet, "task_description", "")
        return (
            HookEvent.PEER_HANDOFF,
            {
                "from_agent": crossing.from_agent,
                "to_agent": crossing.to,
                "task": task[:120] if task else "",
                "depth": crossing.meta.get("depth", 0),
                "parent_run_id": crossing.meta.get("parent_run_id"),
                "child_run_id": crossing.meta.get("child_run_id"),
            },
        )
