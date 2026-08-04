from __future__ import annotations

from prodagent.core.types import ToolCall
from prodagent.guardrail.approval import ContextAwareApprovalFormatter


def test_low_reversibility_shows_NO():
    fmt = ContextAwareApprovalFormatter()
    call = ToolCall(name="delete_records", params={})
    msg = fmt.format(call, reversibility=0.2)
    assert "Reversible : NO" in msg


def test_high_reversibility_shows_YES():
    fmt = ContextAwareApprovalFormatter()
    call = ToolCall(name="restart_pod", params={})
    msg = fmt.format(call, reversibility=0.9)
    assert "Reversible : YES" in msg


def test_default_reversibility_is_NO():
    fmt = ContextAwareApprovalFormatter()
    call = ToolCall(name="some_tool", params={})
    msg = fmt.format(call)
    assert "Reversible : NO" in msg


def test_explicit_extra_reversible_override():
    fmt = ContextAwareApprovalFormatter()
    call = ToolCall(name="update_config", params={})
    msg = fmt.format(call, reversibility=0.2, extra={"reversible": True})
    assert "Reversible : YES" in msg


def test_explicit_extra_reversible_false_override():
    fmt = ContextAwareApprovalFormatter()
    call = ToolCall(name="drop_table", params={})
    msg = fmt.format(call, reversibility=0.9, extra={"reversible": False})
    assert "Reversible : NO" in msg


def test_threshold_boundary_07_shows_YES():
    fmt = ContextAwareApprovalFormatter()
    call = ToolCall(name="x", params={})
    msg = fmt.format(call, reversibility=0.70)
    assert "Reversible : YES" in msg


def test_just_below_threshold_shows_NO():
    fmt = ContextAwareApprovalFormatter()
    call = ToolCall(name="x", params={})
    msg = fmt.format(call, reversibility=0.69)
    assert "Reversible : NO" in msg


def test_none_reversibility_shows_NO():
    fmt = ContextAwareApprovalFormatter()
    call = ToolCall(name="x", params={})
    msg = fmt.format(call, reversibility=None)
    assert "Reversible : NO" in msg
