from __future__ import annotations

import pytest

from prodagent.core.state.run import AgentRun
from prodagent.core.types import ToolCall
from prodagent.evaluation.testing.trace_assert import TrajectoryAssert


def _run(*calls: ToolCall, turns: int = 0) -> AgentRun:
    run = AgentRun(run_id="t", task="test")
    run.tool_history = list(calls)
    run.metrics.turn_count = turns
    return run


def _tc(name: str, **params) -> ToolCall:
    return ToolCall(name=name, params=dict(params))


def test_called_passes_when_tool_present() -> None:
    TrajectoryAssert(_run(_tc("search"))).called("search").assert_all()


def test_called_fails_when_tool_absent() -> None:
    with pytest.raises(AssertionError, match="Expected 'search'"):
        TrajectoryAssert(_run(_tc("ping"))).called("search").assert_all()


def test_never_called_passes_when_tool_absent() -> None:
    TrajectoryAssert(_run(_tc("ping"))).never_called("search").assert_all()


def test_never_called_fails_when_tool_present() -> None:
    with pytest.raises(AssertionError, match="should never have been"):
        TrajectoryAssert(_run(_tc("search"))).never_called("search").assert_all()


def test_then_enforces_ordering() -> None:
    run = _run(_tc("search"), _tc("summarise"))
    TrajectoryAssert(run).called("search").then("summarise").assert_all()


def test_then_fails_on_wrong_order() -> None:
    run = _run(_tc("summarise"), _tc("search"))
    with pytest.raises(AssertionError, match="to be called after 'search'"):
        TrajectoryAssert(run).called("search").then("summarise").assert_all()


def test_call_count_exact() -> None:
    run = _run(_tc("search"), _tc("search"), _tc("ping"))
    TrajectoryAssert(run).call_count("search", exactly=2).assert_all()


def test_call_count_mismatch_fails() -> None:
    run = _run(_tc("search"))
    with pytest.raises(AssertionError, match="expected exactly 2"):
        TrajectoryAssert(run).call_count("search", exactly=2).assert_all()


def test_no_repeated_calls_flags_consecutive() -> None:
    run = _run(_tc("search"), _tc("search"))
    with pytest.raises(AssertionError, match="consecutively"):
        TrajectoryAssert(run).no_repeated_calls("search").assert_all()


def test_no_repeated_calls_passes_when_separated() -> None:
    run = _run(_tc("search"), _tc("ping"), _tc("search"))
    TrajectoryAssert(run).no_repeated_calls("search").assert_all()


def test_called_with_matches_params() -> None:
    run = _run(_tc("refund", order_id="A1", amount=5))
    TrajectoryAssert(run).called_with("refund", order_id="A1").assert_all()


def test_called_with_fails_when_params_dont_match() -> None:
    run = _run(_tc("refund", order_id="A2"))
    with pytest.raises(AssertionError, match="never called with params"):
        TrajectoryAssert(run).called_with("refund", order_id="A1").assert_all()


def test_max_turns_passes_under_budget() -> None:
    TrajectoryAssert(_run(turns=3)).max_turns(5).assert_all()


def test_max_turns_fails_over_budget() -> None:
    with pytest.raises(AssertionError, match="max was 2"):
        TrajectoryAssert(_run(turns=3)).max_turns(2).assert_all()


def test_min_turns_passes() -> None:
    TrajectoryAssert(_run(turns=5)).min_turns(3).assert_all()


def test_min_turns_fails() -> None:
    with pytest.raises(AssertionError, match="min was 5"):
        TrajectoryAssert(_run(turns=3)).min_turns(5).assert_all()


def test_chained_assertions_all_pass() -> None:
    run = _run(_tc("search"), _tc("summarise"), _tc("publish"), turns=3)
    (
        TrajectoryAssert(run)
        .called("search")
        .then("summarise")
        .then("publish")
        .never_called("delete")
        .max_turns(5)
        .assert_all()
    )


def test_first_failure_wins() -> None:
    run = _run(_tc("ping"))
    with pytest.raises(AssertionError, match="Expected 'search'"):
        (TrajectoryAssert(run).called("search").never_called("ping").assert_all())
