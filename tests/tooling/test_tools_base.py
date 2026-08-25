from __future__ import annotations

import asyncio
from enum import Enum
from typing import Literal

from pydantic import BaseModel

from prodagent import SideEffectLevel, ToolMeta
from prodagent.tooling.base import coerce_result
from prodagent.tooling.decorator import _infer_schema, tool


def _sample_fn(count: int, ratio: float, active: bool, label: str) -> dict:
    return {}


def _typed_fn(service: str, replicas: int, dry_run: bool = False) -> dict: ...


def test_infer_schema_description():
    schema = _infer_schema(_typed_fn, "scale_svc", "Scale a service.")
    assert schema["description"] == "Scale a service."
    assert schema["name"] == "scale_svc"


def test_infer_schema_properties():
    schema = _infer_schema(_typed_fn, "scale_svc", "desc")
    props = schema["input_schema"]["properties"]
    assert props["service"]["type"] == "string"
    assert props["replicas"]["type"] == "integer"
    assert props["dry_run"]["type"] == "boolean"


def test_infer_schema_required_excludes_defaults():
    schema = _infer_schema(_typed_fn, "scale_svc", "desc")
    required = schema["input_schema"]["required"]
    assert "service" in required
    assert "replicas" in required
    assert "dry_run" not in required


class _Color(Enum):
    RED = "red"
    BLUE = "blue"


class _Point(BaseModel):
    x: float
    y: float


class _Item(BaseModel):
    name: str


def test_infer_schema_optional_str():
    def fn(label: str | None = None) -> dict: ...

    schema = _infer_schema(fn, "fn", "d")
    prop = schema["input_schema"]["properties"]["label"]
    kinds = {item.get("type") for item in prop.get("anyOf", [])}
    assert "string" in kinds
    assert "null" in kinds
    assert "label" not in schema["input_schema"]["required"]


def test_infer_schema_list_generic():
    def fn(names: list[str]) -> dict: ...

    schema = _infer_schema(fn, "fn", "d")
    prop = schema["input_schema"]["properties"]["names"]
    assert prop["type"] == "array"
    assert prop["items"]["type"] == "string"


def test_infer_schema_dict_kv():
    def fn(scores: dict[str, int]) -> dict: ...

    schema = _infer_schema(fn, "fn", "d")
    prop = schema["input_schema"]["properties"]["scores"]
    assert prop["type"] == "object"
    assert prop["additionalProperties"]["type"] == "integer"


def test_infer_schema_enum():
    def fn(color: _Color = _Color.RED) -> dict: ...

    schema = _infer_schema(fn, "fn", "d")
    prop = schema["input_schema"]["properties"]["color"]
    assert prop["type"] == "string"
    assert set(prop["enum"]) == {"red", "blue"}


def test_infer_schema_literal():
    def fn(mode: Literal["fast", "slow"] = "fast") -> dict: ...

    schema = _infer_schema(fn, "fn", "d")
    prop = schema["input_schema"]["properties"]["mode"]
    assert prop["type"] == "string"
    assert set(prop["enum"]) == {"fast", "slow"}


def test_infer_schema_nested_basemodel():
    def fn(points: list[_Point]) -> dict: ...

    schema = _infer_schema(fn, "fn", "d")
    prop = schema["input_schema"]["properties"]["points"]
    assert prop["type"] == "array"
    item = prop["items"]
    assert item["type"] == "object"
    assert set(item["properties"].keys()) == {"x", "y"}
    assert set(item["required"]) == {"x", "y"}
    assert "$defs" not in schema["input_schema"]
    assert "$ref" not in str(prop)


def test_infer_schema_strips_pydantic_extras():
    def fn(item: _Item) -> dict: ...

    schema = _infer_schema(fn, "fn", "d")
    raw = str(schema)
    assert "title" not in raw
    assert "$defs" not in raw
    assert "default" not in raw


def test_infer_schema_skips_var_positional():
    def fn(base: int, *args: str) -> dict: ...

    schema = _infer_schema(fn, "fn", "d")
    props = schema["input_schema"]["properties"]
    assert "base" in props
    assert "args" not in props


def test_infer_schema_untyped_param_falls_back_to_string():
    def fn(payload): ...

    schema = _infer_schema(fn, "fn", "d")
    prop = schema["input_schema"]["properties"]["payload"]
    assert prop == {"type": "string"}


def test_function_tool_sync_call():
    def greet(name: str) -> str:
        return f"hello {name}"

    ft = tool(name="greet", readonly=True)(greet)
    result = asyncio.run(ft(name="world"))
    assert result.value == "hello world"


def test_function_tool_async_call():
    async def fetch(url: str) -> dict:
        return {"url": url, "status": 200}

    ft = tool(name="fetch_url", readonly=True)(fetch)
    result = asyncio.run(ft(url="http://example.com"))
    assert result.value["status"] == 200


def test_function_tool_coerces_string_to_int():
    def double(n: int) -> int:
        return n * 2

    ft = tool(name="double")(double)
    result = asyncio.run(ft(n="5"))
    assert result.value == 10


def test_function_tool_meta():
    def noop() -> None: ...

    ft = tool(
        name="noop",
        meta=ToolMeta(
            name="noop",
            is_readonly=False,
            side_effect_level=SideEffectLevel.MEDIUM,
            enforced_idempotent=True,
        ),
    )(noop)

    assert ft.name == "noop"
    assert ft.meta.is_readonly is False
    assert ft.meta.side_effect_level == SideEffectLevel.MEDIUM
    assert ft.meta.enforced_idempotent is True


def test_function_tool_schema_name_matches():
    def ping(host: str) -> str: ...

    ft = tool(name="ping")(ping)
    assert ft.schema["name"] == "ping"


def test_tool_error_from_reason_defaults_severity_from_reason_table():
    from prodagent import ToolError
    from prodagent.core.error_reason import ErrorReason
    from prodagent.kernel.types import ErrorSeverity

    err = ToolError.from_reason(
        ErrorReason.FORMAT_ERROR, code="order_not_found", message="no such order", hint="retry"
    )
    assert err.error_severity is ErrorSeverity.RED
    assert err.reason is ErrorReason.FORMAT_ERROR
    assert err.code == "order_not_found"
    assert err.as_dict()["error_severity"] == "red"


def test_tool_error_from_reason_honours_explicit_severity_override():
    from prodagent import ToolError
    from prodagent.core.error_reason import ErrorReason
    from prodagent.kernel.types import ErrorSeverity

    err = ToolError.from_reason(
        ErrorReason.RATE_LIMITED, code="rate_limited", message="429", severity=ErrorSeverity.YELLOW
    )
    assert err.error_severity is ErrorSeverity.YELLOW


def test_tool_result_from_raw_lifts_tool_error():
    from prodagent import ToolError
    from prodagent.core.error_reason import ErrorReason
    from prodagent.kernel.types import ToolOutcome

    err = ToolError.from_reason(ErrorReason.UNKNOWN, code="boom", message="failed")
    tr = coerce_result(err, tool="t")
    assert tr.outcome is ToolOutcome.ABORT
    assert tr.error is not None
    assert tr.error.code == "boom"


def test_tool_result_from_raw_ignores_business_reason_key_without_error_flag():
    from prodagent.kernel.types import ToolOutcome

    raw = {"service": "payment-service", "rolled_back_to": "f8c01d4", "reason": "audit note"}
    tr = coerce_result(raw, tool="rollback")
    assert tr.outcome is ToolOutcome.OK
    assert tr.value == raw


def test_tool_result_from_raw_falls_back_to_unknown_on_invalid_reason_value():
    from prodagent.core.error_reason import ErrorReason
    from prodagent.kernel.types import ToolOutcome

    raw = {"error": True, "reason": "not a real reason", "code": "boom"}
    tr = coerce_result(raw, tool="t")
    assert tr.outcome is ToolOutcome.ABORT
    assert tr.error is not None
    assert tr.error.reason is ErrorReason.UNKNOWN
    assert "not a real reason" in tr.error.message


def test_tool_result_from_raw_uses_string_error_as_message():
    """Tools commonly return ``{"error": "msg", ...}`` (string, not bool) —
    ``coerce_result`` must lift that string into ``ToolError.message`` instead of
    discarding it for the generic ``"invalid ErrorReason: ''"`` fallback."""
    from prodagent.core.error_reason import ErrorReason
    from prodagent.kernel.types import ToolOutcome

    raw = {"placed": False, "error": "proposal PROP-0001 not found"}
    tr = coerce_result(raw, tool="place_order")
    assert tr.outcome is ToolOutcome.ABORT
    assert tr.error is not None
    assert tr.error.reason is ErrorReason.UNKNOWN
    assert tr.error.message == "proposal PROP-0001 not found"


def test_tool_result_from_raw_round_trips_tool_error_as_dict_wire_format():
    from prodagent import ToolError
    from prodagent.core.error_reason import ErrorReason
    from prodagent.kernel.types import ToolOutcome

    wire = ToolError.from_reason(ErrorReason.CONNECTION, code="mcp_transport_error").as_dict()
    tr = coerce_result(wire, tool="t")
    assert tr.outcome is ToolOutcome.RETRY
    assert tr.error is not None
    assert tr.error.reason is ErrorReason.CONNECTION
    assert tr.error.code == "mcp_transport_error"


def test_tool_returning_tool_error_propagates_through_dispatcher():
    import asyncio

    from prodagent import ToolError, tool
    from prodagent.core.error_reason import ErrorReason
    from prodagent.kernel.types import ToolOutcome
    from prodagent.tooling.dispatcher import ToolDispatcher

    @tool(name="fail_permanently")
    async def fail_permanently() -> ToolError:
        return ToolError.from_reason(
            ErrorReason.UNKNOWN, code="bad_state", message="cannot proceed"
        )

    disp = ToolDispatcher({"fail_permanently": fail_permanently}, agent_id="t")
    from prodagent.kernel.types import ToolCall

    raw = asyncio.run(disp.dispatch(ToolCall(name="fail_permanently", params={})))
    tr = coerce_result(raw, tool="fail_permanently")
    assert tr.outcome is ToolOutcome.ABORT


def test_tool_result_from_raw_normalizes_resource_busy_to_retry():
    """A bare resource_busy dict (no explicit severity) stays YELLOW/RETRY —
    severity is derived from the reason, mirroring ToolError.from_reason."""
    from prodagent.core.error_reason import ErrorReason
    from prodagent.kernel.types import ErrorSeverity, ToolOutcome

    raw = {
        "error": True,
        "reason": "resource_busy",
        "code": "resource_busy",
        "message": "Resource 'orders' is busy (held by another agent).",
        "hint": "Try an alternative task or retry later.",
    }
    tr = coerce_result(raw, tool="place_order")
    assert tr.outcome is ToolOutcome.RETRY
    assert tr.error is not None
    assert tr.error.reason is ErrorReason.RESOURCE_BUSY
    assert tr.error.error_severity is ErrorSeverity.YELLOW
    assert tr.error.hint == "Try an alternative task or retry later."


def test_tool_result_from_raw_explicit_severity_wins_over_reason_default():
    from prodagent.kernel.types import ToolOutcome

    raw = {
        "error": True,
        "reason": "resource_busy",
        "error_severity": "red",
        "message": "busy",
    }
    tr = coerce_result(raw, tool="t")
    assert tr.outcome is ToolOutcome.ABORT


def test_tool_result_from_raw_reason_derived_severity_for_retryable_reason():
    from prodagent.core.error_reason import ErrorReason
    from prodagent.kernel.types import ErrorSeverity, ToolOutcome

    raw = {"error": True, "reason": "connection", "message": "conn refused"}
    tr = coerce_result(raw, tool="t")
    assert tr.outcome is ToolOutcome.RETRY
    assert tr.error is not None
    assert tr.error.reason is ErrorReason.CONNECTION
    assert tr.error.error_severity is ErrorSeverity.YELLOW
