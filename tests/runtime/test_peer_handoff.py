from __future__ import annotations

import pytest

from prodagent import Agent, ExecutionMode
from prodagent.core.state.run import is_child_run_id
from prodagent.core.types import LLMResponse
from prodagent.llm.fake import FakeLLMAdapter, script
from prodagent.runtime.coordination.parent_runtime import ParentRuntime
from prodagent.runtime.coordination.peer import PeerPipeline, build_peer_tools_for_agent


@pytest.fixture
def hook_registry():
    from prodagent.hooks.registry import HookRegistry

    return HookRegistry()


def _reactive_agent(name: str, *, context: str = "", peers=None, agents=None) -> Agent:
    return Agent(
        name, system_prompt=context, mode=ExecutionMode.REACTIVE, peers=peers, agents=agents
    )


def test_handoff_tool_schema_per_peer():
    peer = Agent("Summarizer", system_prompt="summarizes text", description="Summarizes text")
    tools = build_peer_tools_for_agent([peer], ctx=ParentRuntime(peer_specs=[peer]))

    assert len(tools) == 1
    schema = tools[0].schema
    assert schema["name"] == "handoff_to_Summarizer"
    assert "task" in schema["input_schema"]["properties"]
    assert schema["input_schema"]["required"] == ["task"]
    assert "name" not in schema["input_schema"]["properties"]


def test_handoff_returns_handoff_result_with_peer_and_task():
    peer = Agent("B")
    pipeline = PeerPipeline([peer], ctx=ParentRuntime(peer_specs=[peer]))

    result = pipeline.handoff("B", task="continue the work", input_refs={"k": "v"})

    assert result.outcome.value == "handoff"
    assert result.handoff == {"peer": "B", "task": "continue the work", "input_refs": {"k": "v"}}


def test_handoff_unknown_peer_returns_abort_error():
    peer = Agent("B")
    pipeline = PeerPipeline([peer], ctx=ParentRuntime(peer_specs=[peer]))

    result = pipeline.handoff("Nonexistent", task="x")

    assert result.outcome.value == "abort"
    assert result.error.code == "unknown_peer"
    assert "Nonexistent" in result.error.message


@pytest.mark.asyncio
async def test_peer_handoff_basic_reactive(hook_registry):
    peer_b = _reactive_agent("B", context="you are B")
    peer_b.config.llm = script({"content": "B says: done!"})

    agent_a = _reactive_agent("A", context="you are A", peers=[peer_b])
    agent_a.config.llm = script({"tool": "handoff_to_B", "params": {"task": "B handle this"}})
    agent_a.config.hooks = hook_registry
    peer_b.config.hooks = hook_registry

    run = await agent_a.chat("start the chain", session_id="peer-basic-reactive")

    assert run.state.value == "completed"
    assert run.final_output == "B says: done!"
    assert is_child_run_id(run.run_id)
    assert run.run_id.endswith("::B")
    assert run.pending_handoff is None
    assert run.is_peer_continuation is True


@pytest.mark.asyncio
async def test_peer_handoff_basic_plan_first(hook_registry):
    peer_b = _reactive_agent("B", context="you are B")
    peer_b.config.llm = script({"content": "B says: plan-first done!"})

    agent_a = Agent("A", system_prompt="you are A", peers=[peer_b])
    plan_json = (
        '{"steps": [{"id": "delegate", "action": "handoff_to_B", '
        '"params": {"task": "handle this"}, "depends_on": [], "is_terminal": true}]}'
    )
    agent_a.config.llm = FakeLLMAdapter(
        responses=[
            LLMResponse(
                content=plan_json, stop_reason="end_turn", input_tokens=10, output_tokens=20
            )
        ],
    )
    agent_a.config.hooks = hook_registry
    peer_b.config.hooks = hook_registry

    run = await agent_a.chat("start the chain", session_id="peer-basic-plan-first")

    assert run.state.value == "completed"
    assert run.final_output == "B says: plan-first done!"
    assert is_child_run_id(run.run_id)


@pytest.mark.asyncio
async def test_peer_no_inherit_messages(hook_registry):
    peer_b = _reactive_agent("B", context="you are B")
    peer_b.config.llm = script({"content": "B done"})
    captured: list[list] = []

    class _CaptureLLM(FakeLLMAdapter):
        async def complete(self, messages, **kw):
            captured.append(list(messages))
            return await super().complete(messages, **kw)

    peer_b.config.llm = _CaptureLLM(
        responses=[LLMResponse(content="B done", stop_reason="end_turn")]
    )

    agent_a = _reactive_agent("A", context="you are A", peers=[peer_b])
    agent_a.config.llm = script({"tool": "handoff_to_B", "params": {"task": "B do X"}})
    agent_a.config.hooks = hook_registry
    peer_b.config.hooks = hook_registry

    await agent_a.chat("start", session_id="peer-no-inherit")

    assert len(captured) == 1
    b_messages = captured[0]
    user_msgs = [m for m in b_messages if m.get("role") == "user"]
    task_msgs = [m for m in user_msgs if "B do X" in m.get("content", "")]
    assert len(task_msgs) == 1
    prompt = task_msgs[0]["content"]
    assert "B do X" in prompt
    assert "Prior agent output" in prompt
    assert "start" not in prompt


@pytest.mark.asyncio
async def test_peer_chain_caps_at_max(hook_registry):
    agents = {}
    for name in ["A", "B", "C", "D", "E", "F"]:
        agent = _reactive_agent(name, context=f"you are {name}")
        agent.config.llm = script(
            {"tool": f"handoff_to_{chr(ord(name) + 1)}", "params": {"task": "next"}}
        )
        agent.config.hooks = hook_registry
        agents[name] = agent

    for name in ["A", "B", "C", "D", "E"]:
        next_name = chr(ord(name) + 1)
        chain_llm = agents[name].config.llm
        agents[name] = _reactive_agent(name, context=f"you are {name}", peers=[agents[next_name]])
        agents[name].config.llm = chain_llm
        agents[name].config.hooks = hook_registry

    agents["F"].config.llm = script({"content": "F final answer"})

    run = await agents["A"].chat("start the chain", session_id="peer-chain-max")

    assert run.state.value == "completed"
    assert run.run_id.count("::") >= 1


@pytest.mark.asyncio
async def test_peer_handoff_with_input_refs(hook_registry):
    peer_b = _reactive_agent("B", context="you are B")
    captured: list[list] = []

    class _CaptureLLM(FakeLLMAdapter):
        async def complete(self, messages, **kw):
            captured.append(list(messages))
            return await super().complete(messages, **kw)

    peer_b.config.llm = _CaptureLLM(
        responses=[LLMResponse(content="B done", stop_reason="end_turn")]
    )

    agent_a = _reactive_agent("A", context="you are A", peers=[peer_b])
    agent_a.config.llm = script(
        {
            "tool": "handoff_to_B",
            "params": {"task": "process this", "input_refs": {"order_record": "orders/123"}},
        }
    )
    agent_a.config.hooks = hook_registry
    peer_b.config.hooks = hook_registry

    await agent_a.chat("start", session_id="peer-input-refs")

    assert len(captured) == 1
    prompt = captured[0][-1]["content"]
    assert "orders/123" in prompt
    assert "order_record" in prompt


@pytest.mark.asyncio
async def test_peer_run_id_uses_child_separator(hook_registry):
    peer_b = _reactive_agent("B")
    peer_b.config.llm = script({"content": "B done"})
    agent_a = _reactive_agent("A", peers=[peer_b])
    agent_a.config.llm = script({"tool": "handoff_to_B", "params": {"task": "go"}})
    agent_a.config.hooks = hook_registry
    peer_b.config.hooks = hook_registry

    run = await agent_a.chat("start", session_id="peer-run-id-sep")

    assert is_child_run_id(run.run_id)
    assert run.run_id.endswith("::B")


@pytest.mark.asyncio
async def test_handoff_unknown_peer_at_runtime_returns_error(hook_registry):
    peer_b = _reactive_agent("B")
    peer_b.config.llm = script({"content": "B done"})
    agent_a = _reactive_agent("A", peers=[peer_b])
    agent_a.config.llm = script(
        {"tool": "handoff_to_Ghost", "params": {"task": "x"}},
        {"tool": "handoff_to_B", "params": {"task": "real"}},
    )
    agent_a.config.hooks = hook_registry
    peer_b.config.hooks = hook_registry

    run = await agent_a.chat("start", session_id="peer-unknown-runtime")

    assert run.state.value == "completed"
    assert "B done" in (run.final_output or "")


@pytest.mark.asyncio
async def test_peer_session_end_fires_for_root_and_peer(hook_registry):
    """SESSION_END must fire for both investigator (root) and peer (continuation).

    Previously the peer's SESSION_END was skipped by memory/learning/security
    because ``is_child_run_id`` saw the ``::`` separator and treated the peer
    as a spawn child. Peer continuations now carry ``is_peer_continuation`` so
    consumers can distinguish them from vertical delegation.
    """
    from prodagent.hooks.events import HookEvent

    fired: list[tuple[str, bool]] = []

    async def _capture(**kw: object) -> None:
        run = kw.get("run")
        rid = str(kw.get("run_id", ""))
        is_peer = getattr(run, "is_peer_continuation", False) if run else False
        fired.append((rid, is_peer))

    hook_registry.register_event(HookEvent.SESSION_END, _capture)

    peer_b = _reactive_agent("B", context="you are B")
    peer_b.config.llm = script({"content": "B done"})
    agent_a = _reactive_agent("A", peers=[peer_b])
    agent_a.config.llm = script({"tool": "handoff_to_B", "params": {"task": "go"}})
    agent_a.config.hooks = hook_registry
    peer_b.config.hooks = hook_registry

    await agent_a.chat("root-task", session_id="peer-session-end")

    peer_entries = [(rid, is_peer) for rid, is_peer in fired if "::B" in rid]
    assert peer_entries, f"peer SESSION_END missing from {fired}"
    assert peer_entries[0][1] is True, "peer run must carry is_peer_continuation=True"


@pytest.mark.asyncio
async def test_spawn_child_is_not_peer_continuation(hook_registry):
    """Spawn children (vertical delegation) must NOT set is_peer_continuation."""
    from prodagent.hooks.events import HookEvent

    fired: list[tuple[str, bool]] = []

    async def _capture(**kw: object) -> None:
        run = kw.get("run")
        rid = str(kw.get("run_id", ""))
        is_peer = getattr(run, "is_peer_continuation", False) if run else False
        fired.append((rid, is_peer))

    hook_registry.register_event(HookEvent.SESSION_END, _capture)

    child = _reactive_agent("child", context="child")
    child.config.llm = script({"content": "child done"})
    agent_a = _reactive_agent("A", agents=[child])
    agent_a.config.llm = script(
        {"tool": "spawn_agent", "params": {"name": "child", "task": "do thing"}},
        {"content": "A done"},
    )
    agent_a.config.hooks = hook_registry
    child.config.hooks = hook_registry

    await agent_a.chat("root", session_id="spawn-not-peer")

    child_entries = [(rid, is_peer) for rid, is_peer in fired if "::child" in rid]
    assert child_entries, f"child SESSION_END missing from {fired}"
    assert child_entries[0][1] is False, "spawn child must not be is_peer_continuation"
