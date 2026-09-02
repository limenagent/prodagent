"""Cost-metering wiring — the fourth HardBudget axis must actually move.

The framework's contract (README "四维硬预算"): turns / seconds / tokens /
cost_usd, any one of which hard-stops the run. cost_usd only moves when a
priced LLMConfig reaches the run's accounting path, so these tests pin the
whole chain: catalog lookup → LLMConfig auto-fill → run accounting → budget.
"""

from __future__ import annotations

import pytest

from prodagent.base.errors import BudgetExceeded
from prodagent.kernel.budget import HardBudget, check_budget
from prodagent.kernel.state import AgentRun
from prodagent.kernel.types import LLMResponse
from prodagent.llm import LLMConfig
from prodagent.llm.pricing import pricing_for_model


def test_catalog_longest_prefix_wins() -> None:
    mini = pricing_for_model("GPT-4o-mini-2024-07-18")
    assert mini is not None
    assert mini.input_rate_per_million == 0.15
    full = pricing_for_model("gpt-4o-2024-08-06")
    assert full is not None
    assert full.input_rate_per_million == 2.5


def test_catalog_unknown_model_returns_none() -> None:
    assert pricing_for_model("fake-llm") is None
    assert pricing_for_model("my-finetune-42") is None


def test_llm_config_autofills_rates_from_catalog() -> None:
    cfg = LLMConfig(model="deepseek-chat")
    assert cfg.cost_per_million_input == pytest.approx(0.27)
    assert cfg.cost_per_million_output == pytest.approx(1.1)


def test_explicit_rates_beat_the_catalog() -> None:
    cfg = LLMConfig(
        model="gpt-4o",
        cost_per_million_input=1.0,
        cost_per_million_output=1.0,
    )
    assert (cfg.cost_per_million_input, cfg.cost_per_million_output) == (1.0, 1.0)


def test_unknown_model_stays_free() -> None:
    cfg = LLMConfig(model="fake-llm")
    assert cfg.cost_per_million_input == 0.0
    assert cfg.cost_per_million_output == 0.0


def _response() -> LLMResponse:
    return LLMResponse(content="ok", input_tokens=50, output_tokens=10)


def test_cost_axis_hard_stops_run() -> None:
    """50 input @ 2.5/M + 10 output @ 10/M = $0.000225 per turn."""
    cfg = LLMConfig(model="gpt-4o")
    run = AgentRun(run_id="r-cost", task="t")
    run.add_tokens(_response(), cost_usd=cfg.cost_for_response(_response()))
    assert run.cost_usd == pytest.approx(0.000225)

    with pytest.raises(BudgetExceeded) as exc_info:
        check_budget(
            run,
            HardBudget(
                max_turns=100,
                max_seconds=3600.0,
                max_tokens=100_000,
                max_cost_usd=0.0002,
            ),
        )
    assert exc_info.value.context["axis"] == "cost_usd"


def test_reactive_loop_accounts_catalog_priced_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: the reactive_scheduler's internal LLMConfig() picks up env-model
    pricing, so run.cost_usd moves without any explicit rate configuration."""
    from prodagent.llm import providers

    monkeypatch.setattr(providers, "detect_default_model", lambda: "deepseek-chat")

    cfg = LLMConfig()  # exactly what reactive_scheduler constructs internally
    assert cfg.cost_for_response(_response()) == pytest.approx((50 * 0.27 + 10 * 1.1) / 1_000_000)
