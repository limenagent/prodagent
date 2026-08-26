from __future__ import annotations

from prodagent.kernel.state import AgentRun, RunMetrics
from prodagent.kernel.types import LLMResponse


def test_add_tokens_accumulates_cache_write_tokens() -> None:
    run = AgentRun(run_id="r1", task="t")
    response = LLMResponse(
        content="hi",
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=30,
        cache_write_tokens=15,
    )
    run.add_tokens(response, cost_usd=0.01)
    run.add_tokens(response, cost_usd=0.01)
    assert run.metrics.cache_write_tokens == 30
    assert run.cache_write_tokens == 30


def test_cache_hit_ratio() -> None:
    metrics = RunMetrics(input_tokens=100, cache_read_tokens=40)
    assert metrics.cache_hit_ratio == 0.4


def test_cache_hit_ratio_with_no_input_tokens_is_zero() -> None:
    assert RunMetrics().cache_hit_ratio == 0.0


def test_run_metrics_round_trips_cache_write_tokens() -> None:
    metrics = RunMetrics(cache_write_tokens=42)
    restored = RunMetrics.from_dict(metrics.to_dict())
    assert restored.cache_write_tokens == 42
