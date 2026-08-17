"""HandoffPacket wire format + IdempotentMessageHandler mechanics (ported from
test_handoff.py when the handoff module dissolved into the messaging plane)."""

from __future__ import annotations

import time

from prodagent.runtime.coordination.messaging.idempotency import IdempotentMessageHandler
from prodagent.runtime.coordination.messaging.packet import HandoffPacket

# ---------------------------------------------------------------- packet


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


# ------------------------------------------------------------- idempotency


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
