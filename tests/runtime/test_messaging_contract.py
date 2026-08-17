"""MessageContract — structural admission semantics (ported from the old
HandoffContract tests) plus the whitelist rewrite that absorbed the old
HandoffInterceptor."""

from __future__ import annotations

from prodagent.runtime.coordination.messaging.contract import (
    DEFAULT_CHILD_CONTRACT,
    MessageContract,
)


def test_contract_accepts_when_required_fields_present():
    c = MessageContract(
        required_fields=["output", "state"], field_types={"output": str, "state": str}
    )
    ok, err = c.validate({"output": "ok", "state": "success"})
    assert ok and err is None


def test_contract_rejects_missing_required_field():
    c = MessageContract(required_fields=["output", "state"])
    ok, err = c.validate({"output": "ok"})
    assert not ok
    assert "state" in err


def test_contract_rejects_wrong_type_on_required_field():
    c = MessageContract(required_fields=["output"], field_types={"output": str})
    ok, err = c.validate({"output": 123})
    assert not ok
    assert "output" in err
    assert "str" in err


def test_contract_validates_optional_field_types_when_present():
    c = MessageContract(
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
    c = MessageContract(required_fields=["state"])
    ok, _ = c.validate({"state": "ok", "metadata": "anything", "cost_usd": 0.0})
    assert ok


def test_contract_strict_default_is_true():
    c = MessageContract(required_fields=["state"])
    assert c.strict is True


def test_contract_value_type_checks_non_mapping_payload():
    c = MessageContract(value_type=str)
    assert c.validate("free text value")[0]
    ok, err = c.validate({"structured": "dict"})
    assert not ok
    assert "str" in err


def test_contract_without_value_type_passes_non_mapping():
    c = MessageContract(required_fields=["state"])
    assert c.validate(object())[0]  # nothing to check on an opaque payload


# -------------------------------------------------------------- whitelist


def test_whitelist_strips_undeclared_keys():
    c = MessageContract(required_fields=["output", "state"])
    view = c.whitelist(
        {
            "output": "ok",
            "state": "success",
            "reasoning": "hidden",
            "thoughts": "also",
            "scratchpad": "x",
        }
    )
    assert view == {"output": "ok", "state": "success"}
    assert "reasoning" not in view
    assert "thoughts" not in view
    assert "scratchpad" not in view


def test_whitelist_includes_declared_optional_fields():
    c = MessageContract(required_fields=["output"], optional_fields=["turns"])
    view = c.whitelist({"output": "ok", "turns": 3, "tool_history": ["leak"]})
    assert view == {"output": "ok", "turns": 3}


def test_default_child_contract_shape():
    ok, err = DEFAULT_CHILD_CONTRACT.validate(
        {"agent": "a", "output": "text", "state": "completed"}
    )
    assert ok, err
    ok, _ = DEFAULT_CHILD_CONTRACT.validate({"output": "text", "state": "completed"})
    assert not ok  # identity is required — parents must know who answered
