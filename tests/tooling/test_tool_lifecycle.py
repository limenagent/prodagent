from __future__ import annotations

import math

import pytest

from prodagent.base.config import ContextConfig
from prodagent.cognition.context.budget import TokenCounter
from prodagent.cognition.context.spill import ToolResultSpillStore
from prodagent.cognition.context.tool_results import reduce_on_append
from prodagent.kernel.types import Message, ToolCall


@pytest.fixture
def cfg():
    return ContextConfig(max_tokens=100_000)


@pytest.fixture
def counter():
    return TokenCounter()


def _tool_msg(content: str, call_id: str = "tc1") -> Message:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


class TestSpillThreshold:
    def test_self_bounding_tool_not_spilled(self, cfg, counter, tmp_path):
        store = ToolResultSpillStore(tmp_path, counter=counter)
        big_content = "x" * 10_000
        call = ToolCall(name="read_tool_result", params={}, call_id="tc1")

        msg = reduce_on_append(
            {"result": big_content}, call, cfg, spill_store=store, max_result_chars=math.inf
        )

        assert store.spill_count == 0
        assert big_content in msg["content"]

    def test_get_skill_ack_not_spilled(self, cfg, counter, tmp_path):
        store = ToolResultSpillStore(tmp_path, counter=counter)
        call = ToolCall(name="get_skill", params={}, call_id="tc1")
        ack = {"skill": "big-skill", "loaded": True, "allowed_tools": []}

        msg = reduce_on_append(ack, call, cfg, spill_store=store)

        assert store.spill_count == 0
        assert msg["role"] == "tool"

    def test_other_tool_results_spilled_when_over_threshold(self, cfg, counter, tmp_path):
        store = ToolResultSpillStore(tmp_path, counter=counter)
        big_content = "x" * 10_000
        call = ToolCall(name="mcp__rca__correlate_alerts", params={}, call_id="tc1")

        msg = reduce_on_append(
            {"result": big_content}, call, cfg, spill_store=store, max_result_chars=2000
        )

        assert msg["content"] != big_content
        assert store.spill_count == 1

    def test_small_result_not_spilled(self, cfg, counter, tmp_path):
        store = ToolResultSpillStore(tmp_path, counter=counter)
        small_content = "x" * 100
        call = ToolCall(name="mcp__rca__x", params={}, call_id="tc1")

        msg = reduce_on_append(
            {"result": small_content}, call, cfg, spill_store=store, max_result_chars=2000
        )

        assert store.spill_count == 0
        assert "x" * 100 in msg["content"]


class TestReduceOnAppend:
    def test_legacy_when_config_none(self):
        call = ToolCall(name="mcp__rca__x", params={}, call_id="tc1")
        wire = {"error": "verbose failure detail " * 50}
        msg = reduce_on_append(wire, call, None)
        assert msg["role"] == "tool"
        assert msg["tool_call_id"] == "tc1"
        assert "verbose failure detail" in msg["content"]

    def test_idempotent_does_not_respill(self, cfg, counter, tmp_path):
        store = ToolResultSpillStore(tmp_path, counter=counter)
        call = ToolCall(name="mcp__rca__x", params={}, call_id="tc1")
        msg1 = reduce_on_append(
            {"result": "x" * 5000}, call, cfg, spill_store=store, max_result_chars=2000
        )
        assert store.spill_count == 1
        reduce_on_append(
            {"result": msg1["content"]}, call, cfg, spill_store=store, max_result_chars=2000
        )
        assert store.spill_count == 1
