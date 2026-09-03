"""kernel.body — the composable-interface vocabulary, on its own terms.

The four things this file pins down: the Outcome defaults (return is the
default control, state deltas are per-instance), the handoff sentinel, the
event tap's FIFO/wake behavior, and that any class with the right ``run``
satisfies ``NodeBody`` structurally — the interface must not require inheritance.
"""

from __future__ import annotations

from typing import Any

from prodagent.kernel.body import (
    HANDOFF_ESCAPED,
    BodyMeta,
    Handoff,
    NodeBody,
    NodeContext,
    Outcome,
    Return,
)


class _Echo:
    """The smallest structural NodeBody: no inheritance, just the method."""

    async def run(self, input: Any, ctx: NodeContext) -> Outcome:
        return Outcome(value=input)


async def test_a_plain_class_with_run_satisfies_unit_structurally() -> None:
    assert isinstance(_Echo(), NodeBody)


async def test_echo_unit_returns_its_input_as_value() -> None:
    ctx = NodeContext(run_id="r1")
    assert (await _Echo().run("hello", ctx)).value == "hello"


def test_outcome_defaults_to_return_control_with_empty_delta() -> None:
    outcome = Outcome()
    assert isinstance(outcome.control, Return)
    assert outcome.state_delta == {}
    assert outcome.value is None


def test_two_outcomes_do_not_share_a_state_delta_list() -> None:
    first, second = Outcome(), Outcome()
    first.state_delta["k"] = 1
    assert second.state_delta == {}


def test_handoff_carries_target_and_defaults_to_full_carry() -> None:
    target = _Echo()
    handoff = Handoff(target=target)
    assert handoff.target is target
    assert handoff.carry == "full"


def test_escaped_outcome_is_recognized_by_sentinel_value() -> None:
    assert Outcome(value=HANDOFF_ESCAPED).escaped() is True
    assert Outcome(value="real output").escaped() is False
    # An abandoned caller has nothing to say — but control explains why.
    abandoned = Outcome(value=HANDOFF_ESCAPED, control=Handoff(target=_Echo()))
    assert abandoned.escaped() is True


async def test_run_context_fire_pushes_only_when_emit_attached() -> None:
    silent = NodeContext(run_id="r1")
    silent.fire("nobody listens")  # no emit — must be a no-op, not an error

    seen: list[Any] = []
    wired = NodeContext(run_id="r2", emit=seen.append)
    wired.fire("event")
    assert seen == ["event"]


def test_unit_meta_defaults_treat_unknown_as_agentic_and_serial() -> None:
    meta = BodyMeta(name="step")
    assert meta.is_agentic is True  # unlabelled = expensive, the safe default
    assert meta.readonly is None  # defer to the registry (tool bodies)
    assert meta.description == ""
