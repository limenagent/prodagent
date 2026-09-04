from __future__ import annotations

from prodagent import Agent, AgentConfig
from prodagent.backends.file import FileDocumentStore, FileGraphStore
from prodagent.base.config import FrameworkConfig
from prodagent.cognition.memory import MemoryProvider
from prodagent.cognition.memory.manager import MemoryManager
from prodagent.hooks.approval import ApprovalGate, ApprovalProvider
from prodagent.hooks.bundles.memory import MemoryHooks
from prodagent.hooks.bundles.security import ApprovalHooks
from prodagent.kernel.bus import HookRegistry
from prodagent.llm.fake import script
from prodagent.runtime.runner import find_approval_gate


def test_find_approval_gate_returns_gate_via_protocol():
    gate = ApprovalGate()
    agent = Agent(
        name="t",
        system_prompt="x",
        config=AgentConfig(
            name="t",
            llm=script({"content": "ok"}),
            hooks=HookRegistry(),
            extensions=[ApprovalHooks(gate=gate)],
        ),
    )
    found = find_approval_gate(agent)
    assert found is gate
    assert isinstance(found, ApprovalProvider)


def test_find_approval_gate_returns_none_without_bundle():
    agent = Agent(
        name="t", system_prompt="x", config=AgentConfig(name="t", llm=script({"content": "ok"}))
    )
    assert find_approval_gate(agent) is None


def test_memory_manager_returns_manager_via_protocol(tmp_path):
    manager = MemoryManager(
        documents=FileDocumentStore(tmp_path),
        facts=FileGraphStore(tmp_path),
    )
    agent = Agent(
        name="t",
        system_prompt="x",
        config=AgentConfig(
            name="t",
            llm=script({"content": "ok"}),
            hooks=HookRegistry(),
            extensions=[MemoryHooks(manager)],
        ),
    )
    found = agent.memory_manager
    assert found is manager
    assert isinstance(found, MemoryProvider)


def test_memory_manager_returns_none_without_bundle():
    agent = Agent(
        name="t", system_prompt="x", config=AgentConfig(name="t", llm=script({"content": "ok"}))
    )
    assert agent.memory_manager is None


def test_memory_manager_none_with_framework_but_no_hooks():
    agent = Agent(
        name="t",
        system_prompt="x",
        config=AgentConfig(
            name="t",
            llm=script({"content": "ok"}),
            framework=FrameworkConfig(),
        ),
    )
    agent.attach_default_hooks()
    assert agent.memory_manager is None
    assert agent.config.memory is None
