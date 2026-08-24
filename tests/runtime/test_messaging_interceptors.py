"""Built-in interceptors — deterministic handoff_data shapes, gate wiring,
trim bounds, projection routing, audit emission."""

from __future__ import annotations

import pytest

from prodagent.coordination.floor import FloorTurn
from prodagent.coordination.floor_projection import PublicTextOnly
from prodagent.coordination.messaging.envelope import (
    Crossing,
    CrossingKind,
    CrossingRejected,
    Direction,
)
from prodagent.coordination.messaging.idempotency import IdempotentMessageHandler
from prodagent.coordination.messaging.interceptors import (
    AuditInterceptor,
    DedupeInterceptor,
    GateInterceptor,
    ProjectionInterceptor,
    TrimInterceptor,
    handoff_data_for,
)
from prodagent.coordination.messaging.packet import HandoffPacket
from prodagent.core.exceptions import SecurityViolation
from prodagent.kernel.bus import HookEvent
from prodagent.kernel.bus import BlockingResult, Gate
from prodagent.kernel.bus import HookRegistry


def _crossing(kind, payload, **overrides):
    kwargs = dict(
        message_id="m-1",
        direction=Direction.UPSTREAM,
        kind=kind,
        from_agent="producer",
        to="consumer",
        payload=payload,
    )
    kwargs.update(overrides)
    return Crossing(**kwargs)


# -------------------------------------------------- deterministic gate shapes


def _handoff_shapes():
    shapes = [
        _crossing(
            CrossingKind.RESULT,
            {"agent": "child", "output": "done", "state": "completed", "turns": 3},
        ),
        _crossing(
            CrossingKind.DISPATCH,
            HandoffPacket(task_description="do the thing"),
            direction=Direction.DOWNSTREAM,
        ),
        _crossing(
            CrossingKind.HANDOFF,
            HandoffPacket(task_description="next hop", prior_output="prior"),
            direction=Direction.DOWNSTREAM,
        ),
        _crossing(CrossingKind.SPEECH, FloorTurn(speaker="mei", round=1, text="你好")),
        _crossing(
            CrossingKind.WRITE,
            _BoardWrite(key="answer", value="42", author="expert-1"),
        ),
        _crossing(
            CrossingKind.TASK_RESULT,
            _WorkResult(item_id="i-1", outcome="success", error=""),
        ),
        _crossing(CrossingKind.ENQUEUE, _WorkItem(item_id="i-1", payload={"q": 1})),
    ]
    return shapes


def test_handoff_data_covers_every_crossing_kind():
    kinds = {c.kind for c in _handoff_shapes()}
    assert kinds == {
        CrossingKind.RESULT,
        CrossingKind.DISPATCH,
        CrossingKind.HANDOFF,
        CrossingKind.SPEECH,
        CrossingKind.WRITE,
        CrossingKind.TASK_RESULT,
        CrossingKind.ENQUEUE,
    }


def test_handoff_data_bounded_by_max_chars():
    crossing = _crossing(CrossingKind.SPEECH, FloorTurn(speaker="a", round=0, text="x" * 500))
    data = handoff_data_for(crossing, max_chars=100)
    assert len(data["result_data"]["text"]) == 100


class _BoardWrite:
    def __init__(self, key, value, author):
        self.key = key
        self.value = value
        self.author = author


class _WorkResult:
    def __init__(self, item_id, outcome, error):
        self.item_id = item_id
        self.outcome = outcome
        self.error = error


class _WorkItem:
    def __init__(self, item_id, payload):
        self.item_id = item_id
        self.payload = payload


# ---------------------------------------------------------------- gate


def _registry(veto: bool) -> HookRegistry:
    registry = HookRegistry()

    async def checker(**data):
        if veto:
            return BlockingResult(blocked=True, reason="policy says no")
        return BlockingResult(blocked=False)

    registry.register_checker(Gate.AGENT_HANDOFF, checker)
    return registry


async def test_gate_is_noop_without_registered_checkers():
    registry = HookRegistry()  # no AGENT_HANDOFF checkers
    gate = GateInterceptor(registry)
    crossing = _crossing(CrossingKind.SPEECH, FloorTurn(speaker="a", round=0, text="hi"))
    assert await gate.intercept(crossing) is crossing


async def test_gate_is_noop_without_hooks():
    gate = GateInterceptor(None)
    crossing = _crossing(CrossingKind.SPEECH, FloorTurn(speaker="a", round=0, text="hi"))
    assert await gate.intercept(crossing) is crossing


async def test_gate_veto_rejects_strictly():
    gate = GateInterceptor(_registry(veto=True))
    crossing = _crossing(CrossingKind.SPEECH, FloorTurn(speaker="a", round=0, text="hi"))
    with pytest.raises(CrossingRejected) as err:
        await gate.intercept(crossing)
    assert err.value.strict is True  # a security refusal is never lenient


async def test_gate_passes_when_checker_allows():
    gate = GateInterceptor(_registry(veto=False))
    crossing = _crossing(CrossingKind.SPEECH, FloorTurn(speaker="a", round=0, text="hi"))
    assert (await gate.intercept(crossing)).payload.text == "hi"


async def test_gate_translates_security_veto_exception():
    registry = HookRegistry()

    async def vetoer(**data):
        raise SecurityViolation("injected instruction detected")

    registry.register_checker(Gate.AGENT_HANDOFF, vetoer)
    gate = GateInterceptor(registry)
    crossing = _crossing(CrossingKind.SPEECH, FloorTurn(speaker="a", round=0, text="hi"))
    with pytest.raises(CrossingRejected, match="security policy rejected"):
        await gate.intercept(crossing)


# ---------------------------------------------------------------- trim


async def test_trim_rewrites_payload():
    trim = TrimInterceptor(lambda p: p[:5])
    crossing = _crossing(CrossingKind.SPEECH, "abcdefghij")
    trimmed = await trim.intercept(crossing)
    assert trimmed.payload == "abcde"


async def test_trim_rejects_when_payload_cannot_be_bounded():
    def unbounded(_):
        return None

    trim = TrimInterceptor(unbounded)
    with pytest.raises(CrossingRejected, match="could not be bounded"):
        await trim.intercept(_crossing(CrossingKind.WRITE, "raw value"))


# ------------------------------------------------------------ projection


async def test_projection_routes_transcript_through_floor_projection():
    projection = PublicTextOnly()
    turns = [
        FloorTurn(
            speaker="other",
            round=0,
            text="long " * 2000,
            tool_calls=[_ToolCall("private_tool")],
        ),
    ]
    interceptor = ProjectionInterceptor("viewer", projection)
    crossing = _crossing(CrossingKind.DISPATCH, list(turns), direction=Direction.DOWNSTREAM)

    delivered = await interceptor.intercept(crossing)

    view = delivered.payload[0]
    assert view.tool_calls == []  # capability leak stripped for the viewer
    assert len(view.text) < 4100  # capped at 4000 + truncation marker
    assert "truncated" in view.text


class _ToolCall:
    def __init__(self, name):
        self.name = name


# ---------------------------------------------------------------- audit


async def test_audit_fires_mapped_event_with_fields():
    registry = HookRegistry()
    seen: list[tuple[str, dict]] = []

    async def handler(**data):
        seen.append((data["event_name"], data))

    registry.register_event(HookEvent.PEER_HANDOFF, handler)
    audit = AuditInterceptor(
        registry,
        lambda c: (HookEvent.PEER_HANDOFF, {"from_agent": c.from_agent, "to_agent": c.to}),
    )

    crossing = _crossing(
        CrossingKind.HANDOFF,
        HandoffPacket(task_description="t"),
        direction=Direction.DOWNSTREAM,
        from_agent="router",
        to="remediator",
    )
    await audit.intercept(crossing)

    assert seen and seen[0][0] == HookEvent.PEER_HANDOFF.value
    assert seen[0][1]["from_agent"] == "router"


async def test_audit_stays_silent_when_mapping_returns_none():
    registry = HookRegistry()
    seen: list[dict] = []

    async def handler(**data):
        seen.append(data)

    registry.register_event(HookEvent.AGENT_RESULT, handler)
    audit = AuditInterceptor(registry, lambda c: None)

    await audit.intercept(_crossing(CrossingKind.RESULT, {"output": "x"}))
    assert seen == []


# ---------------------------------------------------------------- dedupe


async def test_dedupe_wraps_handler():
    handler = IdempotentMessageHandler(ttl_seconds=60.0)
    interceptor = DedupeInterceptor(handler)
    crossing = _crossing(CrossingKind.RESULT, {"output": "x"})

    assert (await interceptor.intercept(crossing)).payload == {"output": "x"}
    with pytest.raises(Exception, match="replayed"):
        await interceptor.intercept(crossing)
