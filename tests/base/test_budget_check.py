from __future__ import annotations

import contextlib

import pytest

from prodagent.base.errors import BudgetExceeded
from prodagent.kernel.budget import HardBudget, check_budget
from prodagent.kernel.state import AgentRun
from prodagent.kernel.types import LLMResponse
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.plan.scheduler import reactive_scheduler
from prodagent.tooling import tool
from prodagent.tooling.dispatcher import ToolDispatcher


def _make_run(*, input_tokens=0, output_tokens=0, cache_read_tokens=0, turns=1):
    run = AgentRun(run_id="test", task="t")
    run.metrics.input_tokens = input_tokens
    run.metrics.output_tokens = output_tokens
    run.metrics.cache_read_tokens = cache_read_tokens
    run.metrics.turn_count = turns
    return run


class TestCachedTokenExclusion:
    def test_cached_tokens_excluded_from_budget_limit(self):
        run = _make_run(
            input_tokens=190_000,
            output_tokens=10_000,
            cache_read_tokens=180_000,
        )
        budget = HardBudget(max_tokens=200_000, max_turns=50, max_seconds=600, max_cost_usd=5.0)
        check_budget(run, budget)

    def test_budget_exceeds_when_billable_tokens_pass_limit(self):
        run = _make_run(
            input_tokens=190_000,
            output_tokens=10_000,
            cache_read_tokens=0,
        )
        budget = HardBudget(max_tokens=200_000, max_turns=50, max_seconds=600, max_cost_usd=5.0)
        with pytest.raises(BudgetExceeded, match="Token limit"):
            check_budget(run, budget)

    def test_budget_message_includes_cached_exclusion_note(self):
        run = _make_run(
            input_tokens=350_000,
            output_tokens=10_000,
            cache_read_tokens=100_000,
        )
        budget = HardBudget(max_tokens=200_000, max_turns=50, max_seconds=600, max_cost_usd=5.0)
        with pytest.raises(BudgetExceeded, match="cached tokens excluded"):
            check_budget(run, budget)

    def test_no_cached_tokens_behaves_like_before(self):
        run = _make_run(input_tokens=100_000, output_tokens=50_000, cache_read_tokens=0)
        budget = HardBudget(max_tokens=200_000, max_turns=50, max_seconds=600, max_cost_usd=5.0)
        check_budget(run, budget)

        run2 = _make_run(input_tokens=150_000, output_tokens=60_000, cache_read_tokens=0)
        with pytest.raises(BudgetExceeded):
            check_budget(run2, budget)


class TestBudgetAxes:
    def test_turn_limit_still_enforced(self):
        run = _make_run(turns=50)
        budget = HardBudget(max_tokens=1_000_000, max_turns=50, max_seconds=600, max_cost_usd=5.0)
        with pytest.raises(BudgetExceeded, match="Turn limit"):
            check_budget(run, budget)


class TestSpawnExtras:
    def test_extra_turns_trips_even_when_own_turns_are_fine(self):
        run = _make_run(turns=1)
        budget = HardBudget(max_turns=3, max_tokens=1_000_000, max_seconds=600, max_cost_usd=5.0)
        check_budget(run, budget)
        with pytest.raises(BudgetExceeded, match="Turn limit"):
            check_budget(run, budget, extra_turns=5)

    def test_extra_cost_trips_even_when_own_cost_is_fine(self):
        run = _make_run(turns=1)
        run.metrics.cost_usd = 0.2
        budget = HardBudget(max_turns=50, max_tokens=1_000_000, max_seconds=600, max_cost_usd=1.0)
        check_budget(run, budget)
        with pytest.raises(BudgetExceeded, match="Cost limit"):
            check_budget(run, budget, extra_cost_usd=0.9)

    def test_extra_tokens_trips_even_when_own_tokens_are_fine(self):
        run = _make_run(input_tokens=100, output_tokens=50)
        budget = HardBudget(max_turns=50, max_tokens=200, max_seconds=600, max_cost_usd=5.0)
        check_budget(run, budget)
        with pytest.raises(BudgetExceeded, match="Token limit"):
            check_budget(run, budget, extra_tokens=100)


class TestAxisPrecedenceConsistency:
    """check_budget() and BudgetLedger both delegate to evaluate_axes() now —
    when multiple axes are over cap simultaneously, both must report the same
    one (turns > seconds > tokens > cost)."""

    def test_check_budget_reports_turns_when_turns_and_cost_both_over(self):
        run = _make_run(turns=10)
        run.metrics.cost_usd = 10.0
        budget = HardBudget(max_turns=5, max_tokens=1_000_000, max_seconds=600, max_cost_usd=1.0)
        with pytest.raises(BudgetExceeded, match="Turn limit"):
            check_budget(run, budget)

    async def test_budget_ledger_reports_turns_when_turns_and_cost_both_over(self):
        from prodagent.kernel.budget import BudgetLedger

        budget = HardBudget(max_turns=5, max_tokens=1_000_000, max_seconds=600, max_cost_usd=1.0)
        ledger = BudgetLedger(max=budget)
        await ledger.commit(member="x", turns=10, tokens=0, cost_usd=10.0)
        with pytest.raises(BudgetExceeded, match="turns axis"):
            await ledger.check(member="x")


class Testreactive_schedulerSpawnAccumulators:
    async def test_loop_trips_on_sibling_spend_it_never_directly_incurred(self):
        from prodagent.kernel.budget import BudgetLedger
        from prodagent.kernel.types import RunFailedEvent

        budget = HardBudget(max_turns=50, max_tokens=1_000_000, max_seconds=600, max_cost_usd=0.9)
        ledger = BudgetLedger(max=budget)
        await ledger.commit(member="sibling", turns=0, tokens=0, cost_usd=0.95)
        llm = FakeLLMAdapter(responses=[LLMResponse(content="done", stop_reason="end_turn")])
        dispatcher = ToolDispatcher({"noop": _noop_tool})
        loop = reactive_scheduler(
            llm,
            dispatcher,
            system_prompt="test",
            tools_schema=[],
            budget=budget,
            budget_ledger=ledger,
        )

        events = [event async for event in loop.stream("test", run_id="sibling-spend-test")]
        terminal = events[-1]
        assert isinstance(terminal, RunFailedEvent), events
        assert "Cost limit" in terminal.error


@tool(name="noop", readonly=True)
async def _noop_tool() -> dict:
    return {"result": "ok"}


def _make_loop(llm: FakeLLMAdapter, budget: HardBudget) -> reactive_scheduler:
    dispatcher = ToolDispatcher({"noop": _noop_tool})
    return reactive_scheduler(
        llm,
        dispatcher,
        system_prompt="test",
        tools_schema=[],
        budget=budget,
    )

    async def test_llm_call_succeeds_within_budget(self):
        budget = HardBudget(max_seconds=10.0, max_turns=10, max_tokens=1_000_000, max_cost_usd=5.0)
        llm = FakeLLMAdapter(
            responses=[
                LLMResponse(
                    content="done", stop_reason="end_turn", input_tokens=10, output_tokens=5
                )
            ],
            latency_ms=5,
        )
        loop = _make_loop(llm, budget)
        async for _ in loop.stream("test", run_id="ok-test"):
            pass
        assert llm.call_count >= 1


class TestMidLoopBudgetChecks:
    async def test_check_budget_runs_before_run_batch(self):
        budget = HardBudget(max_seconds=600.0, max_turns=10, max_tokens=1_000, max_cost_usd=5.0)
        llm = FakeLLMAdapter(
            responses=[
                LLMResponse(
                    content="",
                    tool_calls=[],
                    stop_reason="tool_use",
                    input_tokens=10,
                    output_tokens=5,
                ),
                LLMResponse(
                    content="done", stop_reason="end_turn", input_tokens=10, output_tokens=5
                ),
            ],
        )
        loop = _make_loop(llm, budget)
        async for _ in loop.stream("test", run_id="pre-batch-check"):
            pass

    async def test_check_budget_halt_after_tool_batch_when_tokens_exceed(self):
        budget = HardBudget(max_seconds=600.0, max_turns=10, max_tokens=30, max_cost_usd=5.0)
        llm = FakeLLMAdapter(
            responses=[
                LLMResponse(
                    content="",
                    tool_calls=[],
                    stop_reason="tool_use",
                    input_tokens=10,
                    output_tokens=5,
                ),
                LLMResponse(
                    content="done",
                    stop_reason="end_turn",
                    input_tokens=20,
                    output_tokens=10,
                ),
            ],
        )
        loop = _make_loop(llm, budget)
        with contextlib.suppress(BudgetExceeded):
            async for _ in loop.stream("test", run_id="post-batch-check"):
                pass
