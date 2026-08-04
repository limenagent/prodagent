from __future__ import annotations

import pytest

from prodagent.runtime.agent import Agent
from prodagent.runtime.workflow import Workflow


@pytest.fixture
def hook_registry():
    from prodagent.hooks.registry import HookRegistry

    return HookRegistry()


@pytest.fixture
def fake_llm():
    from prodagent.llm.fake import script

    return script({"content": "should-not-be-called-for-planning"})


@pytest.mark.asyncio
async def test_workflow_runs_dag_and_picks_terminal_output(fake_llm, hook_registry):
    wf = Workflow()

    @wf.step(params={"q": "{{task}}"})
    async def fetch(q: str) -> dict:
        return {"q": q, "hits": 3}

    @wf.step(depends_on=["fetch"])
    async def summarize(fetch: dict) -> str:
        return f"summary of {fetch['q']}: {fetch['hits']} hits"

    @wf.step(depends_on=["summarize"], is_terminal=True)
    async def emit(summarize: str) -> str:
        return summarize

    agent = Agent(
        name="wf-agent",
        context="test",
        llm=fake_llm,
        hooks=hook_registry,
    ).workflow(wf)

    run = await agent.chat("hello world")

    assert run.state.value == "completed"
    assert run.final_output == "{'result': 'summary of hello world: 3 hits'}"


@pytest.mark.asyncio
async def test_workflow_allow_replan_false_caps_max_replans(fake_llm, hook_registry):
    wf = Workflow()

    @wf.step(params={"q": "{{task}}"})
    async def boom(q: str) -> dict:
        raise RuntimeError("intentional failure")

    agent = Agent(
        name="wf-agent",
        context="test",
        llm=fake_llm,
        hooks=hook_registry,
    ).workflow(wf, allow_replan=False)

    assert agent.max_replans == 0

    run = await agent.chat("anything")
    assert run.tool_failures >= 1
    assert run.final_output is None


@pytest.mark.asyncio
async def test_workflow_default_max_replans_is_two(fake_llm, hook_registry):
    wf = Workflow()

    @wf.step(params={"q": "{{task}}"})
    async def fetch(q: str) -> dict:
        return {"q": q}

    agent = Agent(
        name="wf-agent",
        context="test",
        llm=fake_llm,
        hooks=hook_registry,
    ).workflow(wf)

    assert agent.max_replans == 2


def test_workflow_compile_rejects_cycles():
    wf = Workflow()

    @wf.step(depends_on=["b"])
    async def a(b: dict) -> dict:
        return {"a": 1}

    @wf.step(depends_on=["a"])
    async def b(a: dict) -> dict:
        return {"b": 2}

    with pytest.raises(ValueError, match="Cycle detected"):
        wf.compile()


def test_workflow_auto_bind_only_matches_dependency_names():
    wf = Workflow()

    @wf.step
    async def upstream() -> dict:
        return {"x": 1}

    @wf.step(depends_on=["upstream"])
    async def downstream(upstream: dict, free: str) -> dict:
        return {"merged": upstream, "free": free}

    plan = wf.compile()
    step = next(s for s in plan.steps if s.step_id == "downstream")
    assert step.params == {"upstream": "{{upstream.output}}"}
    assert "free" not in step.params


def test_workflow_params_kwarg_overrides_auto_bind():
    wf = Workflow()

    @wf.step
    async def upstream() -> dict:
        return {"x": 1}

    @wf.step(depends_on=["upstream"], params={"upstream": "{{upstream.x}}", "extra": "literal"})
    async def downstream(upstream: int, extra: str) -> dict:
        return {"up": upstream, "ex": extra}

    plan = wf.compile()
    step = next(s for s in plan.steps if s.step_id == "downstream")
    assert step.params == {"upstream": "{{upstream.x}}", "extra": "literal"}


def test_workflow_tool_step_references_existing_tool_no_new_function_tool():
    wf = Workflow()

    @wf.step
    async def fetch() -> dict:
        return {"x": 1}

    wf.tool_step(
        name="route",
        tool_name="some_existing_tool",
        params={"email_id": "eml_001"},
        depends_on=["fetch"],
    )

    plan = wf.compile()
    route_step = next(s for s in plan.steps if s.step_id == "route")
    assert route_step.action == "some_existing_tool"
    assert route_step.params == {"email_id": "eml_001"}
    assert route_step.depends_on == ["fetch"]
    assert [t.name for t in wf.tools] == ["fetch"]


@pytest.mark.asyncio
async def test_workflow_llm_step_binds_lazy_resolved_llm():
    """Real-LLM mode: Agent(llm=None, framework_config=fw).workflow(wf) must
    bind the lazy-resolved LLM (via Agent.llm property), not the raw None
    _llm field. Otherwise llm_step raises "LLM client not bound" at runtime.

    Regression for the fluent.py bug where ``wf._llm = self._llm`` read the
    raw field instead of the property.
    """
    from prodagent.core.config import FrameworkConfig

    fw = FrameworkConfig.default()
    wf = Workflow()
    wf.llm_step("think", prompt="say hi", is_terminal=True)

    # llm=None — simulates real-LLM mode where Agent.llm lazy-resolves from fw.
    agent = Agent(name="wf-lazy-llm", context="test", framework_config=fw).workflow(wf)

    # The workflow's _llm must have been resolved (not None) at .workflow() time.
    assert wf._llm is not None, "workflow._llm must be bound via Agent.llm property"

    run = await agent.chat("hello")
    assert run.state.value == "completed"
