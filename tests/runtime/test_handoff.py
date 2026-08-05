from __future__ import annotations

import time

import pytest

from prodagent.core.exceptions import ContractViolationError
from prodagent.runtime.coordination.handoff import (
    HandoffContract,
    HandoffInterceptor,
    HandoffPacket,
)
from prodagent.runtime.coordination.idempotency import IdempotentMessageHandler


async def test_handler_initially_empty():
    handler = IdempotentMessageHandler(ttl_seconds=10.0)
    assert not await handler.is_duplicate("msg-1")


async def test_handler_marks_and_detects_duplicate():
    handler = IdempotentMessageHandler(ttl_seconds=10.0)
    msg_id = "msg-123"

    assert not await handler.is_duplicate(msg_id)

    assert await handler.is_duplicate(msg_id)

    assert await handler.is_duplicate(msg_id)


async def test_handler_multiple_independent_messages():
    handler = IdempotentMessageHandler(ttl_seconds=10.0)
    assert not await handler.is_duplicate("a")
    assert not await handler.is_duplicate("b")
    assert not await handler.is_duplicate("c")

    assert await handler.is_duplicate("a")
    assert await handler.is_duplicate("b")
    assert await handler.is_duplicate("c")


async def test_handler_ttl_cleanup_expired_entries():
    handler = IdempotentMessageHandler(ttl_seconds=2.0)
    msg_id = "msg-expire"

    assert not await handler.is_duplicate(msg_id)

    assert await handler.is_duplicate(msg_id)

    time.sleep(2.3)

    assert not await handler.is_duplicate(msg_id)


async def test_handler_lazy_cleanup_no_background_thread():
    handler = IdempotentMessageHandler(ttl_seconds=1.0)

    for i in range(10):
        await handler.is_duplicate(f"msg-{i}")

    for i in range(10):
        assert await handler.is_duplicate(f"msg-{i}")

    time.sleep(1.2)

    assert not await handler.is_duplicate("msg-0")


async def test_handler_custom_ttl():
    handler = IdempotentMessageHandler(ttl_seconds=5.0)
    msg_id = "msg-long-ttl"

    assert not await handler.is_duplicate(msg_id)
    time.sleep(0.1)
    assert await handler.is_duplicate(msg_id)


async def test_handler_concurrent_is_duplicate_atomic():
    import asyncio

    handler = IdempotentMessageHandler(ttl_seconds=60.0)

    results = await asyncio.gather(*[handler.is_duplicate("race-msg") for _ in range(10)])

    falses = sum(1 for r in results if r is False)
    trues = sum(1 for r in results if r is True)
    assert falses == 1, f"expected exactly 1 winner, got {falses} (results={results})"
    assert trues == 9, f"expected 9 duplicates, got {trues}"


def test_packet_create_minimal():
    packet = HandoffPacket(task_description="Query database")
    assert packet.task_description == "Query database"
    assert packet.task_id != ""
    assert packet.constraints == []
    assert packet.available_tools == []


def test_packet_create_with_all_fields():
    packet = HandoffPacket(
        task_description="Deploy to prod",
        constraints=["read-only"],
        available_tools=["kubectl", "docker"],
    )
    assert packet.task_description == "Deploy to prod"
    assert packet.constraints == ["read-only"]
    assert packet.available_tools == ["kubectl", "docker"]


def test_packet_create_with_input_refs():
    packet = HandoffPacket(
        task_description="Refund order",
        input_refs={"order_record": "orders/123", "customer": "customers/abc"},
    )
    assert packet.input_refs == {"order_record": "orders/123", "customer": "customers/abc"}


def test_packet_to_task_prompt_renders_input_refs():
    packet = HandoffPacket(
        task_description="Refund order",
        available_tools=["refund", "lookup"],
        input_refs={"order_record": "orders/123"},
    )
    prompt = packet.to_task_prompt()
    assert "Input references" in prompt
    assert "order_record: orders/123" in prompt


def test_packet_to_task_prompt_omits_input_refs_section_when_empty():
    packet = HandoffPacket(task_description="Ping")
    assert "Input references" not in packet.to_task_prompt()


def test_packet_generates_unique_task_ids():
    p1 = HandoffPacket("task 1")
    p2 = HandoffPacket("task 2")
    assert p1.task_id != p2.task_id


def test_packet_generates_unique_message_ids():
    p1 = HandoffPacket("task 1")
    p2 = HandoffPacket("task 2")
    assert p1.message_id != p2.message_id


def test_contract_accepts_when_required_fields_present():
    c = HandoffContract(
        required_fields=["output", "state"], field_types={"output": str, "state": str}
    )
    ok, err = c.validate({"output": "ok", "state": "success"})
    assert ok and err is None


def test_contract_rejects_missing_required_field():
    c = HandoffContract(required_fields=["output", "state"])
    ok, err = c.validate({"output": "ok"})
    assert not ok
    assert "state" in err


def test_contract_rejects_wrong_type_on_required_field():
    c = HandoffContract(required_fields=["output"], field_types={"output": str})
    ok, err = c.validate({"output": 123})
    assert not ok
    assert "output" in err
    assert "str" in err


def test_contract_validates_optional_field_types_when_present():
    c = HandoffContract(
        required_fields=["state"],
        optional_fields=["turns"],
        field_types={"state": str, "turns": int},
    )
    ok, _ = c.validate({"state": "ok"})
    assert ok
    ok, err = c.validate({"state": "ok", "turns": "not-int"})
    assert not ok
    assert "turns" in err


def test_contract_allows_unknown_fields_through():
    c = HandoffContract(required_fields=["state"])
    ok, _ = c.validate({"state": "ok", "metadata": "anything", "cost_usd": 0.0})
    assert ok


def test_contract_strict_default_is_true():
    c = HandoffContract(required_fields=["state"])
    assert c.strict is True


def test_interceptor_strips_reasoning_keys():
    it = HandoffInterceptor()
    c = HandoffContract(required_fields=["output", "state"])
    out = it.intercept(
        {
            "output": "ok",
            "state": "success",
            "reasoning": "hidden",
            "thoughts": "also",
            "scratchpad": "x",
        },
        c,
    )
    assert "reasoning" not in out
    assert "thoughts" not in out
    assert "scratchpad" not in out
    assert out["output"] == "ok"


def test_interceptor_raises_on_contract_violation():
    it = HandoffInterceptor()
    c = HandoffContract(required_fields=["output", "state"], strict=True)
    with pytest.raises(ContractViolationError):
        it.intercept({"output": "ok"}, c)


def test_interceptor_layer_order_strip_before_validate():
    it = HandoffInterceptor()
    c = HandoffContract(
        required_fields=["output"],
        field_types={"output": str},
    )
    out = it.intercept({"output": "ok", "reasoning": ["noisy", "list"]}, c)
    assert "reasoning" not in out
    assert out["output"] == "ok"
