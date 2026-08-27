"""PeerRelay — the hop-by-hop handoff driver for peer chains.

The peer primitive used to be split across two packages: tool schemas in
``coordination/peer.py``, while the actual chain (budget settle-at-handoff,
relay pipeline, checkpoint persistence, peer fork) lived in
``runtime/runner.py``. The chain now lives with the primitive;
``RunLoop`` drives it through the compose seam (``compose.peer_relay``) so
runtime never names coordination outside the assembly root — and the seam is
pure data: the relay returns a :class:`~prodagent.ports.runner.HandoffActivation`,
never a runtime object.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from prodagent.base.errors import BudgetExceeded
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
from prodagent.ports.runner import HandoffActivation

if TYPE_CHECKING:
    from prodagent.kernel.budget import SpawnAccumulator
    from prodagent.ports import CheckpointStore, EventLog
    from prodagent.ports.budget_ledger import BudgetLedgerPort
    from prodagent.runtime.agent import Agent

logger = logging.getLogger(__name__)

__all__ = ["PeerRelay"]


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
