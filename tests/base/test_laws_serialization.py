"""Serialization laws — round-trips are checked as *laws*, not examples.

For every dataclass that crosses a checkpoint / wire boundary, the durable
form must obey::

    load(cls, json.loads(json.dumps(dump(x)))) == x

Going through ``json`` is deliberate: the dict ``dump`` produces is only the
intermediate — what actually persists is JSON text. A pair that round-trips
as dicts but breaks as JSON (NaN, exotic keys) would corrupt checkpoints.

These classes were chosen because they are the persisted vocabulary:
``AgentRun`` (checkpoint), ``LLMResponse`` (response cache), and the codec's
plain mirrors (``ToolCall`` / ``RunMetrics`` / ``PendingHandoff`` /
``ClassifiedError`` / ``TurnRecord`` / ``StoredMemory``).
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass, replace
from typing import TYPE_CHECKING, Any

from hypothesis import given, settings
from hypothesis import strategies as st

from prodagent.base.codec import dump
from prodagent.base.errors import ClassifiedError, ErrorReason
from prodagent.base.session import TurnRecord
from prodagent.base.types import ExecutionMode, RunState
from prodagent.cognition.memory.storage import StoredMemory
from prodagent.kernel.state import AgentRun, PendingHandoff, RunMetrics
from prodagent.kernel.types import LLMResponse, StopReason, ToolCall
from prodagent.ports.persistence import MemoryType

if TYPE_CHECKING:
    from collections.abc import Callable

# JSON-able scalars/containers only: the durable form IS JSON, so the law
# only promises what the persistence layer can actually keep.
_json_scalars = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**53), max_value=2**53)
    | st.floats(allow_nan=False, allow_infinity=False, width=64)
    | st.text(max_size=24)
)
json_values = st.recursive(
    _json_scalars,
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=12), children, max_size=3)
    ),
    max_leaves=12,
)

_one_tool_call = st.builds(
    ToolCall,
    name=st.text(min_size=1, max_size=12),
    params=st.dictionaries(st.text(max_size=8), json_values, max_size=3),
    call_id=st.text(max_size=10),
    metadata=st.dictionaries(st.text(max_size=8), json_values, max_size=2),
)

_tool_calls = st.lists(_one_tool_call, max_size=3)

_error_reasons = st.sampled_from(list(ErrorReason))

_classified = st.builds(
    ClassifiedError,
    reason=_error_reasons,
    code=st.text(max_size=12),
    retryable=st.booleans(),
    status_code=st.none() | st.integers(min_value=400, max_value=599),
    provider=st.text(max_size=8),
    model=st.text(max_size=12),
    raw_message=st.text(max_size=40),
)

_metrics = st.builds(
    RunMetrics,
    turn_count=st.integers(min_value=0, max_value=10**6),
    input_tokens=st.integers(min_value=0, max_value=10**9),
    output_tokens=st.integers(min_value=0, max_value=10**9),
    cache_read_tokens=st.integers(min_value=0, max_value=10**9),
    cache_write_tokens=st.integers(min_value=0, max_value=10**9),
    cost_usd=st.floats(min_value=0, allow_nan=False, allow_infinity=False, width=64),
)

_handoffs = st.builds(
    PendingHandoff,
    peer_name=st.text(max_size=12),
    task=st.text(max_size=24),
    input_refs=st.dictionaries(st.text(max_size=8), st.text(max_size=12), max_size=3),
    prior_output=st.text(max_size=24),
    peer_run_id=st.none() | st.text(max_size=16),
    message_id=st.text(max_size=16),
)

_agent_runs = st.builds(
    AgentRun,
    run_id=st.text(min_size=1, max_size=16),
    task=st.text(max_size=24),
    state=st.sampled_from(list(RunState)),
    metrics=_metrics,
    start_time=st.floats(allow_nan=False, allow_infinity=False, width=64),
    parent_run_id=st.none() | st.text(max_size=16),
    messages=st.lists(st.dictionaries(st.text(max_size=8), json_values, max_size=4), max_size=4),
    tool_history=_tool_calls,
    tool_failures=st.integers(min_value=0, max_value=100),
    last_action=st.none() | st.text(max_size=16),
    retry_counter=st.dictionaries(st.text(max_size=8), st.integers(0, 9), max_size=3),
    fingerprints=st.lists(st.text(max_size=12, alphabet=st.characters(codec="ascii")), max_size=5),
    idempotency_seq=st.integers(min_value=0, max_value=10**6),
    pending_tool_call=st.none() | _one_tool_call,
    pending_approval_id=st.none() | st.text(max_size=12),
    pending_handoff=st.none() | _handoffs,
    last_error=st.none() | st.text(max_size=24),
    error=st.none() | _classified,
    cursors=st.dictionaries(st.text(max_size=8), json_values, max_size=2),
    checkpoint_version=st.integers(min_value=0, max_value=10**4),
    checkpoint_failed=st.booleans(),
    final_output=st.none() | st.text(max_size=24),
    is_peer_continuation=st.booleans(),
)

_llm_responses = st.builds(
    LLMResponse,
    content=st.text(max_size=24),
    tool_calls=_tool_calls,
    stop_reason=st.sampled_from(list(StopReason)),
    input_tokens=st.integers(min_value=0, max_value=10**9),
    output_tokens=st.integers(min_value=0, max_value=10**9),
    model=st.text(max_size=16),
    cache_read_tokens=st.integers(min_value=0, max_value=10**9),
    cache_write_tokens=st.integers(min_value=0, max_value=10**9),
    reasoning_content=st.text(max_size=24),
    thinking_blocks=st.lists(
        st.dictionaries(st.text(max_size=8), json_values, max_size=3), max_size=3
    ),
)

_turn_records = st.builds(
    TurnRecord,
    run_id=st.text(min_size=1, max_size=16),
    mode=st.sampled_from(list(ExecutionMode)),
    state=st.sampled_from(list(RunState)),
    final_output=st.none() | st.text(max_size=24),
    started_at=st.floats(allow_nan=False, allow_infinity=False, width=64),
    ended_at=st.none() | st.floats(allow_nan=False, allow_infinity=False, width=64),
)

_stored_memories = st.builds(
    StoredMemory,
    id=st.text(min_size=1, max_size=16),
    content=st.text(max_size=24),
    memory_type=st.sampled_from(list(MemoryType)),
    domain=st.text(max_size=8),
    entity_id=st.text(max_size=12),
    ttl_days=st.none() | st.integers(min_value=1, max_value=365),
    created_at=st.text(max_size=24),
    superseded=st.booleans(),
    version=st.integers(min_value=1, max_value=99),
    access_count=st.integers(min_value=0, max_value=10**4),
    last_access=st.text(max_size=24),
    embedding=st.none()
    | st.lists(st.floats(allow_nan=False, allow_infinity=False, width=64), max_size=4),
)

_settings = settings(max_examples=150, deadline=None)


def _assert_roundtrip(value: Any, revive: Callable[[dict[str, Any]], Any]) -> None:
    as_json = json.dumps(dump(value), default=str)
    assert revive(json.loads(as_json)) == value


@_settings
@given(_one_tool_call)
def test_tool_call_roundtrip_law(call: ToolCall) -> None:
    _assert_roundtrip(call, ToolCall.from_dict)


@_settings
@given(_metrics)
def test_run_metrics_roundtrip_law(metrics: RunMetrics) -> None:
    _assert_roundtrip(metrics, RunMetrics.from_dict)


@_settings
@given(_handoffs)
def test_pending_handoff_roundtrip_law(handoff: PendingHandoff) -> None:
    _assert_roundtrip(handoff, PendingHandoff.from_dict)


@_settings
@given(_classified)
def test_classified_error_roundtrip_law(err: ClassifiedError) -> None:
    _assert_roundtrip(err, ClassifiedError.from_dict)


@_settings
@given(_turn_records)
def test_turn_record_roundtrip_law(record: TurnRecord) -> None:
    _assert_roundtrip(record, TurnRecord.from_dict)


@_settings
@given(_stored_memories)
def test_stored_memory_roundtrip_law(mem: StoredMemory) -> None:
    _assert_roundtrip(mem, StoredMemory.from_dict)


@_settings
@given(_agent_runs)
def test_agent_run_checkpoint_roundtrip_law(run: AgentRun) -> None:
    """The checkpoint law: what to_dict persists, from_dict rebuilds exactly —
    schema v2's boxed cursors included.

    Two deliberate exceptions, both ownership choices the law forced into the
    open: ``checkpoint_version`` is the store envelope's echo (assigned on
    save, restored from the envelope on load — never the run's own persisted
    fact), and ``checkpoint_failed`` is a process-local sticky diagnostic
    (the durable record is the CHECKPOINT_FAILED event; a resumed run starts
    with a clean flag and re-observes)."""
    as_json = json.dumps(run.to_dict(), default=str)
    revived = AgentRun.from_dict(json.loads(as_json))
    assert revived == replace(run, checkpoint_version=0, checkpoint_failed=False)
    assert revived.checkpoint_version == 0
    assert revived.checkpoint_failed is False
    # And the durable subset is genuinely JSON-able (no str() fallback needed
    # beyond tool_history's nested params).
    assert isinstance(as_json, str)


@_settings
@given(_llm_responses)
def test_llm_response_cache_roundtrip_law(resp: LLMResponse) -> None:
    """The response-cache law: a cached LLMResponse thaws into itself."""
    as_json = json.dumps(resp.to_dict(), default=str)
    assert LLMResponse.from_dict(json.loads(as_json)) == resp


@_settings
@given(_metrics)
def test_codec_dump_is_dataclass_shaped(metrics: RunMetrics) -> None:
    """dump() must emit every declared field — a field that silently drops
    out of the durable form is a checkpoint that loads wrong."""
    assert set(dump(metrics)) == set(asdict(metrics))
    assert is_dataclass(metrics)
