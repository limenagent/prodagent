"""Wave-B behaviour guards — one regression test per refactor decision.

Each test pins a craft fix so the old failure mode cannot quietly return:
- fork field propagation (was: hand-copied field list that silently dropped
  new AgentConfig fields)
- Workflow single-binding (was: re-binding silently rewired compiled plans)
- streaming retry never replays delivered chunks
- backend-kind env validation fails fast on typos
"""

from __future__ import annotations

from typing import Any

import pytest

from prodagent import Agent, AgentConfig
from prodagent.runtime.parent_runtime import ParentRuntime
from prodagent.core.config import FrameworkConfig
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.llm.http_retry import DeliveryGuard, with_http_retry
from prodagent.plan.workflow import Workflow


def _agent(**kwargs: Any) -> Agent:
    base = dict(
        name="forky",
        system_prompt="test",
        llm=FakeLLMAdapter(),
        description="carried by propagation",
    )
    base.update(kwargs)
    return Agent(base["name"], config=AgentConfig(**base))


class TestForkPropagation:
    def test_fork_propagates_fields_the_old_skeleton_dropped(self) -> None:
        """description/output_contract/spill_store propagate automatically —
        the hand-copied list this replaces forgot them."""
        agent = _agent()
        runtime = ParentRuntime(
            llm=agent.config.llm,
            hooks=None,
            framework_config=FrameworkConfig.default(),
            constraints=[],
            budget=None,
            checkpoint=None,
            event_log=None,
        )
        forked = agent.fork_as_spawn(runtime)
        assert forked.config.description == "carried by propagation"
        assert forked.config.system_prompt == "test"

    def test_fork_runtime_overrides_win(self) -> None:
        agent = _agent()
        runtime = ParentRuntime(
            llm=None,
            hooks=None,
            framework_config=FrameworkConfig.default(),
            constraints=["override"],
            budget=None,
            checkpoint=None,
            event_log=None,
        )
        forked = agent.fork_as_spawn(runtime)
        assert forked.config.constraints == ["override"]


class TestWorkflowSingleBinding:
    def test_rebinding_a_different_llm_is_refused(self) -> None:
        wf = Workflow()
        wf.bind(FakeLLMAdapter(), None)
        with pytest.raises(ValueError, match="already bound"):
            wf.bind(FakeLLMAdapter(), None)  # a second, different client

    def test_idempotent_rebind_of_same_client_is_allowed(self) -> None:
        llm = FakeLLMAdapter()
        wf = Workflow()
        wf.bind(llm, None)
        wf.bind(llm, None)  # same client — no-op, no raise


class TestStreamRetryNeverReplays:
    async def test_failure_after_first_delivery_is_not_retried(self) -> None:
        """A mid-stream failure after the consumer received output must
        propagate instead of retrying — a retry would replay delivered chunks."""
        guard = DeliveryGuard()
        attempts = 0

        async def flaky() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                guard.mark()
                raise ConnectionError("mid-stream drop")
            return "would-replay"

        with pytest.raises(ConnectionError):
            await with_http_retry(flaky, stream_guard=guard)
        assert attempts == 1, "delivered stream must not be retried"

    async def test_failure_before_delivery_still_retries(self) -> None:
        guard = DeliveryGuard()
        attempts = 0

        async def flaky() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionError("connection refused before any output")
            return "ok"

        from prodagent.core.retry import RetryPolicy

        result = await with_http_retry(
            flaky,
            stream_guard=guard,
            policy=RetryPolicy(max_attempts=3, base_delay=0.01),
        )
        assert result == "ok"
        assert attempts == 2


class TestBackendEnvValidation:
    def test_unknown_backend_kind_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRODAGENT_BACKEND_GRAPH", "mysql")
        with pytest.raises(ValueError, match="PRODAGENT_BACKEND_GRAPH"):
            FrameworkConfig.from_env()

    def test_per_backend_override_applies_without_prod_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PRODAGENT_BACKEND", raising=False)
        monkeypatch.setenv("PRODAGENT_BACKEND_CACHE", "redis")
        fw = FrameworkConfig.from_env()
        assert fw.backend.cache == "redis"
        assert fw.backend.checkpoint == "file"
