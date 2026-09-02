from __future__ import annotations

from prodagent import RunState
from prodagent.base.errors import ClassifiedError, ErrorReason
from prodagent.kernel.state import AgentRun
from prodagent.kernel.types import ToolCall


def _rich_run() -> AgentRun:
    run = AgentRun(run_id="r1", task="refund order 42")
    run.state = RunState.SUSPENDED
    run.messages = [
        {"role": "user", "content": "refund it"},
        {"role": "assistant", "content": "calling refund tool"},
    ]
    run.tool_history = [ToolCall(name="refund", params={"order": 42}, call_id="c1")]
    run.metrics.cost_usd = 0.05
    run.metrics.turn_count = 3
    run.metrics.input_tokens = 100
    run.metrics.output_tokens = 40
    run.tool_failures = 1
    run.last_action = "refund"
    run.retry_counter = {"refund": 2}
    run.fingerprints = ["fp1", "fp2", "fp3"]
    run.idempotency_seq = 7
    run.pending_tool_call = ToolCall(name="refund", params={"order": 42}, call_id="c1")
    run.last_error = "403 Forbidden"
    run.error = ClassifiedError(reason=ErrorReason.AUTH_FORBIDDEN, retryable=False, status_code=403)
    run.set_cursor("plan", {"state": {"nodes": {}, "version": 3}, "last_seq": 7})
    return run


def test_full_round_trip_preserves_every_field():
    original = _rich_run()
    restored = AgentRun.from_dict(original.to_dict())

    assert restored.run_id == "r1"
    assert restored.task == "refund order 42"
    assert restored.state is RunState.SUSPENDED
    assert restored.messages == original.messages
    assert restored.cost_usd == 0.05
    assert restored.turn_count == 3
    assert restored.input_tokens == 100
    assert restored.output_tokens == 40
    assert restored.tool_failures == 1
    assert restored.last_action == "refund"
    assert restored.retry_counter == {"refund": 2}
    assert restored.fingerprints == ["fp1", "fp2", "fp3"]
    assert restored.idempotency_seq == 7
    assert restored.last_error == "403 Forbidden"
    assert restored.error is not None
    assert restored.error.reason is ErrorReason.AUTH_FORBIDDEN
    assert restored.error.retryable is False
    assert restored.error.status_code == 403
    assert restored.cursor("plan") == {"state": {"nodes": {}, "version": 3}, "last_seq": 7}


def test_pending_tool_call_survives():
    restored = AgentRun.from_dict(_rich_run().to_dict())
    assert restored.pending_tool_call is not None
    assert restored.pending_tool_call.name == "refund"
    assert restored.pending_tool_call.params == {"order": 42}
    assert restored.pending_tool_call.call_id == "c1"


def test_tool_history_rehydrates_to_toolcalls():
    restored = AgentRun.from_dict(_rich_run().to_dict())
    assert len(restored.tool_history) == 1
    assert isinstance(restored.tool_history[0], ToolCall)
    assert restored.tool_history[0].name == "refund"


def test_clean_run_has_no_crash_scene():
    run = AgentRun(run_id="r2", task="hi")
    restored = AgentRun.from_dict(run.to_dict())
    assert restored.last_error is None
    assert restored.error is None
    assert restored.pending_tool_call is None
    assert restored.fingerprints == []
    assert restored.idempotency_seq == 0


def test_start_time_survives_round_trip():
    run = AgentRun(run_id="r3", task="resume me")
    run.start_time = 1_000_000.0
    restored = AgentRun.from_dict(run.to_dict())
    assert restored.start_time == 1_000_000.0
