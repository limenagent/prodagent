"""Wire model round-trips — the data-model unit's two schemas.

Unit 2 of arch-review-2026-08-27-atomicity-verdict.md: AgentSpec (the "who
runs" document) and AgentEvent's wire codec (the "what happened" stream)
must survive a to_wire/from_wire cycle losslessly for JSON-able payloads.
The v1-checkpoint migration regression lives in
tests/runtime/test_settlement_convergence.py.
"""

from __future__ import annotations

import json

from prodagent import Agent, ExecutionMode
from prodagent.kernel.budget import HardBudget
from prodagent.kernel.state import AgentRun
from prodagent.kernel.types import (
    NodeCompletedEvent,
    NodeFailedEvent,
    NodeStartedEvent,
    RunCompletedEvent,
    RunFailedEvent,
    RunSuspendedEvent,
    ThinkTokenEvent,
    ToolCall,
    ToolCallStartEvent,
    ToolResultEvent,
)
from prodagent.ports.agent_events import event_from_wire, event_to_wire
from prodagent.ports.execution import AgentSpec, spec_from_any

# ---------------------------------------------------------------- AgentSpec


def test_agent_spec_round_trips_with_nesting_and_budget() -> None:
    spec = AgentSpec(
        name="root",
        description="does root things",
        system_prompt="be root",
        mode=ExecutionMode.PLAN_FIRST,
        constraints=["no prod writes"],
        budget=HardBudget(max_turns=7, max_seconds=99.0, max_tokens=1234, max_cost_usd=0.5),
        tools_schema=[{"name": "t1", "input_schema": {"type": "object"}}],
        max_replans=0,
        child_agents=[AgentSpec(name="child", description="a child")],
        peers=[AgentSpec(name="peer", system_prompt="peer prompt")],
    )
    restored = AgentSpec.from_dict(spec.to_dict())
    assert restored == spec
    # budget reconstructed as a real HardBudget, not a dict
    assert isinstance(restored.budget, HardBudget)
    assert restored.budget.max_turns == 7
    assert restored.child_agents[0].name == "child"
    assert restored.peers[0].system_prompt == "peer prompt"


def test_agent_spec_defaults_round_trip() -> None:
    spec = AgentSpec(name="bare")
    assert AgentSpec.from_dict(spec.to_dict()) == spec
    assert json.dumps(spec.to_dict())  # JSON-able by construction


def test_live_agent_projects_to_spec() -> None:
    from prodagent.runtime.config import AgentConfig

    agent = Agent(
        "parent",
        system_prompt="delegate everything",
        budget=HardBudget(max_turns=3),
        config=AgentConfig(name="parent", constraints=["be cheap"]),
    )
    spec = agent.spec()
    assert spec.name == "parent"
    assert spec.system_prompt == "delegate everything"
    assert spec.budget is not None and spec.budget.max_turns == 3
    # projection round-trips like any spec
    assert AgentSpec.from_dict(spec.to_dict()) == spec
    # spec_from_any accepts both shapes
    assert spec_from_any(agent) == spec
    assert spec_from_any(spec) is spec


def test_spec_describe_prefers_description_then_prompt() -> None:
    assert AgentSpec(name="a", description="the desc").describe() == "the desc"
    long_prompt = "x" * 100
    described = AgentSpec(name="a", system_prompt=long_prompt).describe()
    assert described.startswith("x") and described.endswith("...")
    assert AgentSpec(name="a").describe() == ""


# --------------------------------------------------------------- event codec


def test_every_event_type_round_trips_through_the_wire() -> None:
    run = AgentRun(run_id="r1", task="t")
    run.metrics.turn_count = 2
    events = [
        ThinkTokenEvent(token="hi", run_id="r1"),
        ToolCallStartEvent(call=ToolCall(name="search", params={"q": "x"}), run_id="r1"),
        ToolResultEvent(name="search", result={"hits": [1, 2]}, run_id="r1"),
        NodeStartedEvent(node_id="s1", action="search", run_id="r1"),
        NodeCompletedEvent(node_id="s1", action="search", result="done", run_id="r1"),
        NodeFailedEvent(node_id="s1", action="search", error="boom", run_id="r1"),
        RunCompletedEvent(run=run),
        RunFailedEvent(run=run, error="budget"),
        RunSuspendedEvent(run=run),
    ]
    for event in events:
        wire = event_to_wire(event)
        assert json.dumps(wire), f"{type(event).__name__} wire form must be JSON-able"
        restored = event_from_wire(wire)
        assert restored == event, f"{type(event).__name__} must round-trip"


def test_run_events_carry_the_run_as_the_checkpoint_document() -> None:
    run = AgentRun(run_id="wire-run", task="t")
    run.set_cursor("plan", {"state": {"nodes": {}}, "last_seq": 4})
    wire = event_to_wire(RunCompletedEvent(run=run))
    assert wire["run"]["cursors"]["plan"]["last_seq"] == 4
    restored = event_from_wire(wire)
    assert isinstance(restored, RunCompletedEvent)
    assert restored.run.cursor("plan") == {"state": {"nodes": {}}, "last_seq": 4}


def test_opaque_payload_stringifies_instead_of_failing() -> None:
    class Opaque:
        def __str__(self) -> str:
            return "<opaque>"

    wire = event_to_wire(ToolResultEvent(name="t", result=Opaque(), run_id="r"))
    assert wire["result"] == "<opaque>"
    assert json.dumps(wire)


def test_wire_rejects_unknown_event_types() -> None:
    import pytest

    with pytest.raises(ValueError):
        event_from_wire({"type": "NotAnEvent"})
