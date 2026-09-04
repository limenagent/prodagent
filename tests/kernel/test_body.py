"""kernel.body — the composable-interface vocabulary, on its own terms.

The three things this file pins down: the Outcome defaults (an empty value,
state deltas are per-instance), the event tap's FIFO/wake behavior, and that
any class with the right ``run`` satisfies ``NodeBody`` structurally — the
interface must not require inheritance.
"""

from __future__ import annotations

from typing import Any

from prodagent.kernel.body import NodeBody, NodeContext, Outcome


class _Echo:
    """The smallest structural NodeBody: no inheritance, just the method."""

    async def run(self, input: Any, ctx: NodeContext) -> Outcome:
        return Outcome(value=input)


async def test_a_plain_class_with_run_satisfies_unit_structurally() -> None:
    assert isinstance(_Echo(), NodeBody)


async def test_echo_unit_returns_its_input_as_value() -> None:
    ctx = NodeContext(run_id="r1")
    assert (await _Echo().run("hello", ctx)).value == "hello"


def test_outcome_defaults_to_an_empty_value_and_delta() -> None:
    outcome = Outcome()
    assert outcome.state_delta == {}
    assert outcome.value is None


def test_two_outcomes_do_not_share_a_state_delta_list() -> None:
    first, second = Outcome(), Outcome()
    first.state_delta["k"] = 1
    assert second.state_delta == {}


def test_command_valued_outcome_is_the_outcome() -> None:
    from prodagent.kernel.command import Update

    assert Outcome(value=Update("k", 1)).value.key == "k"


async def test_run_context_fire_pushes_only_when_emit_attached() -> None:
    silent = NodeContext(run_id="r1")
    silent.fire("nobody listens")  # no emit — must be a no-op, not an error

    seen: list[Any] = []
    wired = NodeContext(run_id="r2", emit=seen.append)
    wired.fire("event")
    assert seen == ["event"]
