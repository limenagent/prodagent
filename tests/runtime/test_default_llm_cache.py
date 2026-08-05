from __future__ import annotations

import pytest

from prodagent import Agent, ExecutionMode
from prodagent.core.types import LLMResponse
from prodagent.llm.base import LLMConfig, noop_chunk
from prodagent.llm.cache import CachingLLMClient


class _CountingLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, *, system="", tools=None, config=None, on_chunk):
        self.calls += 1
        return LLMResponse(
            content=f"resp-{self.calls}",
            input_tokens=10,
            output_tokens=10,
            model="test",
        )


def _make_messages(content: str = "hi") -> list[dict]:
    return [{"role": "user", "content": content}]


class TestDefaultCacheWiring:
    def test_resolve_llm_wraps_with_caching_client(self):
        from prodagent.runtime.session import _resolve_llm

        agent = Agent("t", system_prompt="x", llm=_CountingLLM())
        llm = _resolve_llm(agent)
        assert isinstance(llm, CachingLLMClient)

    def test_user_supplied_caching_client_not_double_wrapped(self):
        from prodagent.runtime.session import _resolve_llm

        inner = _CountingLLM()
        user_cached = CachingLLMClient(inner, None)  # type: ignore[arg-type]
        agent = Agent("t", system_prompt="x", llm=user_cached)
        llm = _resolve_llm(agent)
        assert llm is user_cached

    def test_plain_llm_is_wrapped_not_misclassified_as_caching(self):
        from prodagent.llm.cache import CachingLLM
        from prodagent.runtime.session import _resolve_llm

        plain = _CountingLLM()
        assert not isinstance(plain, CachingLLM)

        agent = Agent("t", system_prompt="x", llm=plain)
        llm = _resolve_llm(agent)
        assert isinstance(llm, CachingLLMClient)
        assert llm is not plain

    async def test_intra_run_cache_hit_skips_billing(self):
        llm = _CountingLLM()
        agent = Agent("billing", system_prompt="x", llm=llm, mode=ExecutionMode.REACTIVE)
        from prodagent.runtime.session import _resolve_llm

        wrapped = _resolve_llm(agent)
        cfg = LLMConfig(model="m", temperature=0.0, max_tokens=100)
        msgs = _make_messages("same")

        r1 = await wrapped.complete(msgs, config=cfg, on_chunk=noop_chunk)
        r2 = await wrapped.complete(msgs, config=cfg, on_chunk=noop_chunk)

        assert llm.calls == 1
        assert r1.from_cache is False
        assert r2.from_cache is True

    async def test_temperature_gt_zero_bypasses_default_cache(self):
        llm = _CountingLLM()
        from prodagent.runtime.session import _resolve_llm

        agent = Agent("t", system_prompt="x", llm=llm)
        wrapped = _resolve_llm(agent)

        cfg = LLMConfig(model="m", temperature=0.7, max_tokens=100)
        msgs = _make_messages("creative")

        await wrapped.complete(msgs, config=cfg, on_chunk=noop_chunk)
        await wrapped.complete(msgs, config=cfg, on_chunk=noop_chunk)

        assert llm.calls == 2

    def test_resolve_llm_does_not_mutate_agent_llm_field(self):
        """Agent._llm stays declarative — RunContext owns the resolved client.

        A None _llm must remain None after resolution so the Agent can be
        reused across runs without leaking a resolved client back into
        declarative state.
        """
        from prodagent.runtime.session import _resolve_llm

        agent = Agent("t", system_prompt="x")
        assert agent.config.llm is None
        resolved = _resolve_llm(agent)
        assert isinstance(resolved, CachingLLMClient)
        assert agent.config.llm is None

        declared = _CountingLLM()
        agent_with_llm = Agent("t", system_prompt="x", llm=declared)
        _resolve_llm(agent_with_llm)
        assert agent_with_llm.config.llm is declared

    async def test_context_resolves_checkpoint_and_event_log_without_mutating_agent(self):
        """RunContext.__aenter__ resolves checkpoint/event_log without writing
        them back to the Agent — the Agent stays declarative across runs."""
        from prodagent.core.config import FrameworkConfig
        from prodagent.runtime.session import RunContext

        agent = Agent("t", system_prompt="x", framework=FrameworkConfig.default())
        assert agent.config.checkpoint is None
        assert agent.config.event_log is None
        async with RunContext(agent=agent, task="t", run_id="r1") as ctx:
            assert ctx.checkpoint is not None
            assert ctx.event_log is not None
        assert agent.config.checkpoint is None
        assert agent.config.event_log is None


class TestCostSkipping:
    def test_post_llm_accounting_skips_cached_response(self):
        from prodagent.core.state.run import AgentRun

        run = AgentRun(run_id="r1", task="t")

        from prodagent.runtime.reactive import AgentLoop

        loop = object.__new__(AgentLoop)
        loop._llm_config = LLMConfig(model="m")
        loop._hooks = None
        loop._budget = None

        cached_resp = LLMResponse(
            content="cached",
            input_tokens=50,
            output_tokens=50,
            model="m",
            from_cache=True,
        )
        import asyncio

        asyncio.run(AgentLoop._post_llm_accounting(loop, run, cached_resp))  # type: ignore[arg-type]

        assert run.turn_count == 1
        assert run.input_tokens == 0
        assert run.output_tokens == 0
        assert run.cost_usd == 0.0

    def test_post_llm_accounting_bills_fresh_response(self):
        from prodagent.core.state.run import AgentRun

        run = AgentRun(run_id="r1", task="t")
        from prodagent.runtime.reactive import AgentLoop

        loop = object.__new__(AgentLoop)
        loop._llm_config = LLMConfig(
            model="m",
            cost_per_million_input=1.0,
            cost_per_million_output=2.0,
        )
        loop._hooks = None
        loop._budget = None

        fresh_resp = LLMResponse(
            content="fresh",
            input_tokens=100,
            output_tokens=50,
            model="m",
        )
        import asyncio

        asyncio.run(AgentLoop._post_llm_accounting(loop, run, fresh_resp))  # type: ignore[arg-type]

        assert run.turn_count == 1
        assert run.input_tokens == 100
        assert run.output_tokens == 50
        assert run.cost_usd == pytest.approx(0.0002, rel=1e-6)
