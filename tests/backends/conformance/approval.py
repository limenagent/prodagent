"""Conformance tests for ``ApprovalStore`` implementations."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeAlias

from prodagent.ports.observability import ApprovalDecision, ApprovalRequest, ApprovalStore

Factory: TypeAlias = Callable[[], ApprovalStore]


def _req(rid: str = "ap1") -> ApprovalRequest:
    return ApprovalRequest(
        request_id=rid,
        tool_name="place_order",
        params={"qty": 10},
        context_summary="buy 10 shares",
    )


async def run_approval_conformance(make_store: Factory) -> None:
    store = make_store()

    assert await store.get_request("missing") is None

    req = _req()
    await store.create_request(req)
    loaded = await store.get_request("ap1")
    assert loaded is not None
    assert loaded.request_id == "ap1"
    assert loaded.decision is None, "fresh request has no decision"


async def run_approval_idempotent_create_conformance(make_store: Factory) -> None:
    """Re-creating the same request_id is a no-op (checkpoint reload case)."""
    store = make_store()
    await store.create_request(_req())
    await store.create_request(_req())  # must not raise
    loaded = await store.get_request("ap1")
    assert loaded is not None


async def run_approval_decision_flow_conformance(make_store: Factory) -> None:
    store = make_store()
    await store.create_request(_req())

    await store.submit_decision("ap1", ApprovalDecision.APPROVE, approver_id="alice")
    decided = await store.get_request("ap1")
    assert decided is not None
    assert decided.decision == ApprovalDecision.APPROVE
    assert decided.approver_id == "alice"
    assert decided.decided_at is not None


async def run_approval_decision_overwrite_conformance(make_store: Factory) -> None:
    """A second submit for the same request_id overwrites the first."""
    store = make_store()
    await store.create_request(_req())
    await store.submit_decision("ap1", ApprovalDecision.APPROVE)
    await store.submit_decision("ap1", ApprovalDecision.REJECT, approver_id="bob")
    loaded = await store.get_request("ap1")
    assert loaded is not None
    assert loaded.decision == ApprovalDecision.REJECT
    assert loaded.approver_id == "bob"
