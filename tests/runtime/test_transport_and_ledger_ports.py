"""Port #14 (Transport) and #15 (BudgetLedgerPort) — conformance + seam.

Two new ports from the G0 pre-work:

- ``Transport`` (ports/transport.py): one boundary direction of the message
  plane as a ``send(crossing) -> Delivery`` endpoint. The in-process
  implementation is ``PipelineTransport`` (coordination/messaging/transport.py);
  spawn's dispatch/result pair and the peer relay build through the shared
  ``build_transport`` factory so preset selection and dedupe TTL policy
  cannot drift between primitives.
- ``BudgetLedgerPort`` (ports/budget_ledger.py): the four-axis settlement
  vocabulary. The kernel's ``BudgetLedger`` satisfies it structurally — a
  remote arbiter can swap in without touching the kernel envelope or the
  coordination primitives.
"""

from __future__ import annotations

import pytest

from prodagent.coordination.messaging.envelope import Crossing, CrossingKind, Direction
from prodagent.coordination.messaging.transport import (
    PipelineTransport,
    TransportSpec,
    build_transport,
)
from prodagent.kernel.budget import BudgetLedger, HardBudget, run_enveloped
from prodagent.kernel.bus import HookEvent, HookRegistry
from prodagent.ports import BudgetLedgerPort, SpendView, Transport


def _crossing(message_id: str = "") -> Crossing[dict]:
    return Crossing.mint(
        direction=Direction.DOWNSTREAM,
        kind=CrossingKind.DISPATCH,
        from_agent="a",
        to="b",
        payload={"task": "go"},
        message_id=message_id,
    )


# ----------------------------------------------------------------- Transport


def test_pipeline_transport_satisfies_the_transport_port():
    transport = build_transport(TransportSpec(direction=Direction.DOWNSTREAM))
    assert isinstance(transport, PipelineTransport)
    assert isinstance(transport, Transport)


async def test_transport_send_returns_delivery_verdicts():
    transport = build_transport(TransportSpec(direction=Direction.DOWNSTREAM))

    delivery = await transport.send(_crossing())

    assert delivery.delivered
    assert delivery.crossing.payload == {"task": "go"}


async def test_transport_dedupe_ttl_suppresses_replays_by_message_id():
    transport = build_transport(
        TransportSpec(direction=Direction.DOWNSTREAM, dedupe_ttl_s=600.0)
    )

    first = await transport.send(_crossing(message_id="mid-1"))
    replay = await transport.send(_crossing(message_id="mid-1"))

    assert first.delivered
    assert replay.status == "duplicate"
    assert replay.stage == "dedupe"


async def test_transport_audit_fires_only_for_delivered_crossings():
    hooks = HookRegistry()
    audits: list[str] = []

    async def _audit(**kw):
        audits.append(kw["to_agent"])

    hooks.register_event(HookEvent.AGENT_SPAWN, _audit)
    transport = build_transport(
        TransportSpec(
            direction=Direction.DOWNSTREAM,
            dedupe_ttl_s=600.0,
            hooks=hooks,
            audit_event=lambda c: (HookEvent.AGENT_SPAWN, {"to_agent": c.to}),
        )
    )

    await transport.send(_crossing(message_id="m1"))
    await transport.send(_crossing(message_id="m1"))  # duplicate — no audit

    assert audits == ["b"]


def test_upstream_spec_mounts_contract_admission_downstream_does_not():
    downstream = build_transport(TransportSpec(direction=Direction.DOWNSTREAM))
    upstream = build_transport(
        TransportSpec(
            direction=Direction.UPSTREAM,
            contract=lambda c: None,
            trim=lambda p: p,
        )
    )

    downstream_map = downstream.pipeline.describe()
    upstream_map = upstream.pipeline.describe()

    assert "contract" in upstream_map
    assert "contract" not in downstream_map  # downstream container IS the whitelist


# ------------------------------------------------------------- BudgetLedgerPort


def test_kernel_budget_ledger_satisfies_the_port():
    ledger = BudgetLedger(max=HardBudget(max_turns=5))
    assert isinstance(ledger, BudgetLedgerPort)


async def test_settlement_vocabulary_through_the_port_type():
    """The full reserve/check/commit/release cycle through a port-typed
    reference — a remote arbiter implements the same vocabulary."""
    ledger: BudgetLedgerPort = BudgetLedger(max=HardBudget(max_turns=3, max_tokens=100))

    await ledger.reserve(member="alice", turns=1, tokens=40)
    assert isinstance(ledger.spent, SpendView)
    assert ledger.spent.tokens == 40
    assert ledger.member_reserved("alice").tokens == 40

    await ledger.commit(
        member="alice", turns=1, tokens=40, cost_usd=0.1, reserved_turns=1, reserved_tokens=40
    )
    assert ledger.committed.tokens == 40
    assert ledger.member_reserved("alice").tokens == 0

    await ledger.release(member="bob", reserved_turns=5)  # not bob's to release
    assert ledger.spent.turns == 1

    assert ledger.elapsed_seconds() >= 0.0
    assert not ledger.is_exhausted()


async def test_run_enveloped_accepts_any_port_implementation():
    """The kernel envelope settles through the port, not the concrete class —
    a fake remote arbiter drives it just the same."""

    class RecordingLedger(BudgetLedger):
        """Local subclass stands in for an alternative implementation."""

    ledger = RecordingLedger(max=HardBudget(max_turns=2))

    async def _act() -> tuple[int, int, float]:
        return (1, 10, 0.05)

    settled = await run_enveloped(ledger, member="alice", act=_act)

    assert settled == (1, 10, 0.05)
    assert ledger.committed.tokens == 10


def test_port_surface_is_complete_for_the_coordination_primitives():
    """Spawn/relay/stage drivers use exactly these members — pin the roster so
    the port cannot silently fall behind the concrete class."""
    expected = {
        "max",
        "committed",
        "spent",
        "member_reserved",
        "elapsed_seconds",
        "is_exhausted",
        "check",
        "reserve",
        "release",
        "commit",
    }
    assert expected <= set(dir(BudgetLedgerPort))
