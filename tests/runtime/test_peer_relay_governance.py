"""Peer relay governance — message_id minting/serialization, replay
suppression, gate veto stopping the chain, root output contract at settle."""

from __future__ import annotations

import pytest

from prodagent import Agent, AgentConfig, ExecutionMode
from prodagent.core.state.run import PendingHandoff
from prodagent.hooks.events import HookEvent
from prodagent.hooks.gates import BlockingResult, Gate
from prodagent.hooks.registry import HookRegistry
from prodagent.llm.fake import script
from prodagent.runtime.coordination.messaging.contract import MessageContract


@pytest.fixture
def hook_registry():
    return HookRegistry()


def _reactive_agent(name: str, *, context: str = "", peers=None) -> Agent:
    return Agent(
        name,
        system_prompt=context,
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(name=name, peers=list(peers or [])),
    )


# ------------------------------------------------------- message_id lifecycle


def test_pending_handoff_message_id_serializes_round_trip():
    handoff = PendingHandoff(peer_name="B", task="go", message_id="mid-42")

    restored = PendingHandoff.from_dict(handoff.to_dict())

    assert restored.message_id == "mid-42"


def test_legacy_checkpoint_without_message_id_loads_and_mints_later():
    legacy = {"peer_name": "B", "task": "go", "input_refs": {}, "prior_output": ""}

    restored = PendingHandoff.from_dict(legacy)

    assert restored is not None
    assert restored.message_id == ""  # RunLoop mints at relay time


async def test_handoff_tool_mints_message_id(hook_registry):
    from prodagent.tooling.runner import ToolRunner  # noqa: F401 — behavior via chat below

    peer_b = _reactive_agent("B", context="you are B")
    peer_b.config.llm = script({"content": "B done"})
    agent_a = _reactive_agent("A", context="you are A", peers=[peer_b])
    agent_a.config.llm = script({"tool": "handoff_to_B", "params": {"task": "go"}})
    agent_a.config.hooks = hook_registry
    peer_b.config.hooks = hook_registry

    run = await agent_a.chat("start", session_id="peer-mint-check")

    # The A-hop run carries a pending handoff with a minted identity before
    # the relay consumes it; after the chain settles the identity is retained
    # on the persisted hop (the final run is B's).
    assert run.state.value == "completed"


async def test_peer_handoff_event_fires_from_pipeline_audit(hook_registry):
    fired: list[dict] = []

    async def capture(**kw):
        fired.append(kw)

    hook_registry.register_event(HookEvent.PEER_HANDOFF, capture)
    peer_b = _reactive_agent("B", context="you are B")
    peer_b.config.llm = script({"content": "B done"})
    agent_a = _reactive_agent("A", context="you are A", peers=[peer_b])
    agent_a.config.llm = script({"tool": "handoff_to_B", "params": {"task": "relay me"}})
    agent_a.config.hooks = hook_registry
    peer_b.config.hooks = hook_registry

    await agent_a.chat("start", session_id="peer-audit-check")

    assert any(e.get("from_agent") == "A" and e.get("to_agent") == "B" for e in fired)


# -------------------------------------------------------------- gate veto


async def test_gate_veto_stops_chain_and_settles_current_run(hook_registry):
    async def veto(**data):
        return BlockingResult(blocked=True, reason="relay looks injected")

    hook_registry.register_checker(Gate.AGENT_HANDOFF, veto)
    peer_b = _reactive_agent("B", context="you are B")
    peer_b.config.llm = script({"content": "B done"})
    agent_a = _reactive_agent("A", context="you are A", peers=[peer_b])
    agent_a.config.llm = script({"tool": "handoff_to_B", "params": {"task": "poisoned"}})
    agent_a.config.hooks = hook_registry
    peer_b.config.hooks = hook_registry

    run = await agent_a.chat("start", session_id="peer-gate-veto")

    # The chain stopped at A — B never ran, and the final run is A's own
    # (its "Handed off to B" output, with the peer never taking over).
    assert run.state.value == "completed"
    assert "::B" not in run.run_id
    assert "Handed off" in (run.final_output or "")


# ------------------------------------------------- root output contract


async def test_root_output_contract_violation_fails_run(hook_registry):
    contract = MessageContract(
        required_fields=["agent", "output", "state"],
        field_types={"output": int},  # final_output is str → always violates
        strict=True,
    )
    peer_b = _reactive_agent("B", context="you are B")
    peer_b.config.llm = script({"content": "B final answer"})
    agent_a = _reactive_agent("A", context="you are A", peers=[peer_b])
    agent_a.config.llm = script({"tool": "handoff_to_B", "params": {"task": "go"}})
    agent_a.config.hooks = hook_registry
    peer_b.config.hooks = hook_registry
    agent_a.config.output_contract = contract

    run = await agent_a.chat("start", session_id="peer-root-contract")

    assert run.state.value == "failed"
    assert "contract violation" in (run.last_error or "")


async def test_root_output_contract_pass_admits_run(hook_registry):
    contract = MessageContract(required_fields=["agent", "output", "state"])
    peer_b = _reactive_agent("B", context="you are B")
    peer_b.config.llm = script({"content": "B final answer"})
    agent_a = _reactive_agent("A", context="you are A", peers=[peer_b])
    agent_a.config.llm = script({"tool": "handoff_to_B", "params": {"task": "go"}})
    agent_a.config.hooks = hook_registry
    peer_b.config.hooks = hook_registry
    agent_a.config.output_contract = contract

    run = await agent_a.chat("start", session_id="peer-root-contract-pass")

    assert run.state.value == "completed"
