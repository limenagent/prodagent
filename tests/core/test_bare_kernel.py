"""Bare-kernel acceptance — falsifiable evidence for the positioning claim.

"Lightweight" is not a slogan; each test here pins one observable fact:

1. a default Agent writes ZERO files — the naked kernel touches no disk;
2. the production preset restores today's full stack in one call;
3. the published core stays 4 dependencies and ships no test-only code;
4. bare semantics: no default bundles, no cache wrap, REACTIVE default,
   in-process multi-turn, submit_approval is an explicit error.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from prodagent import Agent, AgentConfig, ExecutionMode, script
from prodagent.core.exceptions import UnknownApprovalError

REPO_ROOT = Path(__file__).resolve().parents[2]


def _bare_agent() -> Agent:
    return Agent(
        "bare",
        system_prompt="reply briefly.",
        tools=[],
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(name="bare", llm=script({"content": "done"})),
    )


async def test_hello_world_writes_zero_files(tmp_path, monkeypatch):
    """A default Agent run in a fresh cwd leaves the directory empty."""
    monkeypatch.chdir(tmp_path)
    agent = _bare_agent()
    run = await agent.chat("hello")
    assert run.state.value == "completed"
    assert not (tmp_path / ".prodagent").exists()
    assert list(tmp_path.rglob("*")) == [], f"bare kernel wrote files: {list(tmp_path.rglob('*'))}"


async def test_bare_multi_turn_session_stays_in_memory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = Agent(
        "chat",
        system_prompt="reply briefly.",
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(
            name="chat",
            llm=script({"content": "first"}, {"content": "second: seen first"}),
        ),
    )
    r1 = await agent.chat("turn one", session_id="s1")
    r2 = await agent.chat("turn two", session_id="s1")
    assert r1.run_id != r2.run_id  # same session, distinct turns
    assert r2.state.value == "completed"
    assert not (tmp_path / ".prodagent").exists()


async def test_bare_submit_approval_is_explicit_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = _bare_agent()
    await agent.chat("hello", session_id="s")
    with pytest.raises(UnknownApprovalError):
        await agent.submit_approval("whatever", "approve")


def test_bare_default_mode_is_reactive():
    assert AgentConfig(name="x").mode is ExecutionMode.REACTIVE


def test_bare_bundles_exclude_observers_and_gate():
    from prodagent.core.config import FrameworkConfig
    from prodagent.hooks.bundles.base import default_hook_bundles
    from prodagent.hooks.bundles.default_wiring import (
        ApprovalDefaultBundle,
        CacheMonitorDefaultBundle,
        SpanDefaultBundle,
    )

    bundles = default_hook_bundles(FrameworkConfig.default())
    types = {type(b) for b in bundles}
    assert ApprovalDefaultBundle not in types
    assert SpanDefaultBundle not in types
    assert CacheMonitorDefaultBundle not in types


def test_bare_llm_is_not_wrapped():
    from prodagent.coordination.run_loop import _resolve_llm
    from prodagent.llm.cache import CachingLLMClient

    llm = script({"content": "x"})
    agent = _bare_agent()
    agent.config.llm = llm
    assert _resolve_llm(agent) is llm
    assert not isinstance(_resolve_llm(agent), CachingLLMClient)


async def test_production_restores_the_full_stack(tmp_path, monkeypatch):
    from prodagent.coordination.run_loop import _resolve_llm
    from prodagent.core.config import FrameworkConfig, production
    from prodagent.core.types import SideEffectLevel, ToolMeta
    from prodagent.hooks.approval import ApprovalGate
    from prodagent.llm.cache import CachingLLMClient
    from prodagent.tooling import tool

    monkeypatch.chdir(tmp_path)
    fw = production(FrameworkConfig.default())
    fw.orchestration.spans_path = str(tmp_path / "spans.jsonl")
    fw.orchestration.runs_dir = str(tmp_path / "runs")
    fw.orchestration.sessions_dir = str(tmp_path / "sessions")
    fw.orchestration.events_dir = str(tmp_path / "events")
    assert fw.profile == "production"
    assert fw.context.compression is True
    assert fw.context.spill_tool_results is True

    @tool(
        name="destroy",
        meta=ToolMeta(name="destroy", side_effect_level=SideEffectLevel.HIGH),
    )
    async def destroy(target: str) -> dict:
        return {"destroyed": target}

    agent = Agent(
        "prod",
        system_prompt="use the tool",
        tools=[destroy],
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(
            name="prod",
            llm=script({"tool": "destroy", "params": {"target": "db"}}),
            framework=fw,
        ),
    )
    # LLM wrapped, approval gate present
    assert isinstance(_resolve_llm(agent), CachingLLMClient)
    agent.attach_default_hooks()
    assert isinstance(agent._find_approval_gate(), ApprovalGate)

    run = await agent.chat("destroy the db", session_id="prod-1")
    assert run.state.value == "suspended"  # HIGH tool hit the gate
    assert run.pending_approval_id

    await agent.submit_approval(run.pending_approval_id, "approve")
    run = await agent.chat(resume=True, session_id="prod-1")
    assert run.state.value == "completed"

    assert (tmp_path / "spans.jsonl").exists(), "production profile must export spans"
    assert any(tmp_path.glob("runs/*.json")), "production profile must checkpoint runs"


def test_published_core_dependencies_stay_thin():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    deps = pyproject["project"]["dependencies"]
    assert deps == [
        "anyio>=4.4.0",
        "httpx>=0.27.0",
        "pydantic>=2.7.0",
        "typing-extensions>=4.12.0",
    ], f"core dependency set drifted: {deps}"
    raw = (REPO_ROOT / "pyproject.toml").read_text()
    assert "qdrant" not in raw and "numpy" not in raw


def test_conformance_suites_do_not_ship_in_the_wheel():
    src_suite = REPO_ROOT / "src" / "prodagent" / "backends" / "conformance"
    tests_suite = REPO_ROOT / "tests" / "backends" / "conformance"
    assert not src_suite.exists(), "conformance code must live under tests/, not the wheel"
    assert tests_suite.exists()
