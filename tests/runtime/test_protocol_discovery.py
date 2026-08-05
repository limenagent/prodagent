from __future__ import annotations

from prodagent import Agent, ExecutionMode
from prodagent.backends.file import FileDocumentStore, FileGraphStore
from prodagent.cognition.memory import MemoryProvider
from prodagent.cognition.memory.manager import MemoryManager
from prodagent.guardrail.approval import ApprovalGate, ApprovalProvider
from prodagent.hooks.bundles.memory import MemoryHooks
from prodagent.hooks.bundles.security import ApprovalHooks
from prodagent.hooks.registry import HookRegistry
from prodagent.llm.fake import script


def test_find_approval_gate_returns_gate_via_protocol():
    gate = ApprovalGate()
    agent = Agent(
        name="t",
        system_prompt="x",
        llm=script({"content": "ok"}),
        hooks=HookRegistry(),
        mode=ExecutionMode.REACTIVE,
        extensions=[ApprovalHooks(gate=gate)],
    )
    found = agent._find_approval_gate()
    assert found is gate
    assert isinstance(found, ApprovalProvider)


def test_find_approval_gate_returns_none_without_bundle():
    agent = Agent(name="t", system_prompt="x", llm=script({"content": "ok"}))
    assert agent._find_approval_gate() is None


def test_memory_manager_returns_manager_via_protocol(tmp_path):
    manager = MemoryManager(
        documents=FileDocumentStore(tmp_path),
        facts=FileGraphStore(tmp_path),
    )
    agent = Agent(
        name="t",
        system_prompt="x",
        llm=script({"content": "ok"}),
        hooks=HookRegistry(),
        mode=ExecutionMode.REACTIVE,
        extensions=[MemoryHooks(manager)],
    )
    found = agent.memory_manager
    assert found is manager
    assert isinstance(found, MemoryProvider)


def test_memory_manager_returns_none_without_bundle():
    agent = Agent(name="t", system_prompt="x", llm=script({"content": "ok"}))
    assert agent.memory_manager is None
