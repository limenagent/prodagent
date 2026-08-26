"""PeerRelay — the hop-by-hop handoff driver for peer chains.

The peer primitive used to be split across two packages: tool schemas in
``coordination/peer.py``, while the actual chain (budget settle-at-handoff,
relay pipeline, checkpoint persistence, peer fork) lived in
``runtime/runner.py``. The chain now lives with the primitive;
``RunLoop`` drives it through the compose seam (``compose.peer_relay``) so
runtime never names coordination outside the assembly root.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from prodagent.coordination.messaging.envelope import Crossing, CrossingKind, Direction
from prodagent.coordination.messaging.packet import HandoffPacket
from prodagent.coordination.messaging.transport import (
    PipelineTransport,
    TransportSpec,
    build_transport,
)
from prodagent.core.exceptions import BudgetExceeded
from prodagent.kernel.bus import HookEvent, save_and_fire_checkpoint
from prodagent.kernel.state import AgentRun, child_run_id
from prodagent.runtime.parent_runtime import hop_own_share
from prodagent.runtime.runner import RunContext

if TYPE_CHECKING:
    from prodagent.ports.budget_ledger import BudgetLedgerPort

if TYPE_CHECKING:
    from prodagent.runtime.parent_runtime import SpawnAccumulator

logger = logging.getLogger(__name__)

__all__ = ["PeerRelay"]


class PeerRelay:
    """Owns the relay pipeline (dedupe spans the whole chain) and decides,
    for a run that parked a handoff, whether and where the chain continues."""

    def __init__(self, root_run_id: str) -> None:
        self._root_run_id = root_run_id
        self._transport: PipelineTransport | None = None

    async def next_context(
        self,
        ctx: RunContext,
        run: AgentRun,
        spawn_acc: SpawnAccumulator | None = None,
        ledger: BudgetLedgerPort | None = None,
    ) -> RunContext | None:
        """The next hop's RunContext, or ``None`` when the chain stops here."""
        if run.pending_handoff is None:
            return None
        handoff = run.pending_handoff
        fw = ctx.agent.framework_config
        if ctx.depth >= fw.orchestration.max_peer_chain:
            return None

        peer_name = handoff.peer_name
        peer_spec = ctx.agent.peer_named(peer_name)
        if peer_spec is None:
            logger.error(
                "[orchestrator] peer %r not found on agent %r — chain stops",
                peer_name,
                ctx.agent.name,
            )
            return None

        if ledger is not None:
            # Commit only this hop's OWN share: children already committed
            # live, and the fold folded their totals into run.metrics —
            # committing the post-fold numbers would count them twice.
            own_turns, own_tokens, own_cost = hop_own_share(run, spawn_acc)
            await ledger.commit(
                member=ctx.agent.name,
                turns=own_turns,
                tokens=own_tokens,
                cost_usd=own_cost,
            )
            try:
                await ledger.check(member=peer_name)
            except BudgetExceeded as exc:
                logger.warning(
                    "[orchestrator] peer chain budget exhausted before handoff %s → %s: %s",
                    ctx.agent.name,
                    peer_name,
                    exc,
                )
                return None

        prior_output = run.final_output or ""
        packet = HandoffPacket(
            task_description=handoff.task,
            constraints=list(ctx.agent.constraints),
            available_tools=[t.name for t in peer_spec.inline_tools],
            input_refs=handoff.input_refs or {},
            prior_output=prior_output,
        )
        if not handoff.message_id:
            handoff.message_id = str(uuid.uuid4())  # checkpoint written pre-migration
        peer_run_id = child_run_id(self._root_run_id, peer_name)
        handoff.peer_run_id = peer_run_id  # persist on the run before save below

        delivery = await self._transport_for(ctx).send(
            Crossing.mint(
                direction=Direction.DOWNSTREAM,
                kind=CrossingKind.HANDOFF,
                from_agent=ctx.agent.name,
                to=peer_name,
                payload=packet,
                trace_id=self._root_run_id,
                message_id=handoff.message_id,
                depth=ctx.depth + 1,
                parent_run_id=ctx.run_id,
                child_run_id=peer_run_id,
            )
        )
        if delivery.status != "delivered":
            # A duplicate relay (checkpointed handoff replayed in-process) or a
            # gate veto — the chain stops here and the current run settles.
            logger.warning(
                "[orchestrator] handoff %s → %s not delivered (%s): %s",
                ctx.agent.name,
                peer_name,
                delivery.status,
                delivery.reason,
            )
            return None

        if ctx.checkpoint is not None:
            await save_and_fire_checkpoint(ctx.checkpoint, run, ctx.agent.hooks)

        return RunContext(
            agent=peer_spec.fork_as_peer(
                ctx.agent,
                ctx.run_id,
                checkpoint=ctx.checkpoint,
                event_log=ctx.event_log,
            ),
            task=packet.to_task_prompt(),
            run_id=peer_run_id,
            depth=ctx.depth + 1,
            parent_run_id=ctx.run_id,
        )

    def _transport_for(self, ctx: RunContext) -> PipelineTransport:
        """The relay's DOWNSTREAM transport, built on first handoff.

        Dedupe is shared across the whole chain (one handler per relay);
        hooks are read at first relay, when executor preparation has attached
        them. Built through the shared transport factory like every other
        boundary; the PEER_HANDOFF audit event fires from the pipeline's last
        slot — only for crossings that were actually delivered.
        """
        if self._transport is None:
            fw = ctx.agent.framework_config
            orch = fw.orchestration if fw is not None else None
            ttl = orch.handoff_idempotency_ttl_s if orch is not None else 600.0
            self._transport = build_transport(
                TransportSpec(
                    direction=Direction.DOWNSTREAM,
                    dedupe_ttl_s=ttl,
                    hooks=ctx.agent.hooks,
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
