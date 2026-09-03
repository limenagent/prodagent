"""Planner rebinding across forks — the spec-built child's dead-llm trap.

A spec-built child (peer/spawn roster) captures a placeholder llm at
construction — often ``None``. The fork supplies the parent's live client;
the config-time planner must follow, or the child drafts against a dead
reference and every planning call dies with ``'NoneType' object has no
attribute 'complete'``. Regression for the playground aiops handoff.
"""

from __future__ import annotations

import json

from prodagent import Agent, AgentConfig
from prodagent.kernel.types import LLMResponse
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.plan.planner import Planner
from prodagent.runtime.parent_runtime import ParentRuntime


def _plan_llm() -> FakeLLMAdapter:
    return FakeLLMAdapter(
        responses=[LLMResponse(content=json.dumps({"steps": []}), stop_reason="end_turn")]
    )


def _child_with_dead_planner() -> Agent:
    """The trap's shape: spec-built child, planner bound to a None llm."""
    return Agent(
        "remediator",
        system_prompt="fix incidents",
        config=AgentConfig(
            name="remediator",
            llm=None,  # spec-built: wired at fork time, not here
            planner=Planner(None),  # type: ignore[arg-type] — the captured dead reference
        ),
    )


def _runtime(llm) -> ParentRuntime:
    return ParentRuntime(
        llm=llm,
        hooks=None,
        framework_config=None,
        constraints=[],
        budget=None,
        checkpoint=None,
        event_log=None,
    )


class TestForkRebindsPlanner:
    def test_fork_as_spawn_rebinds_the_planner_to_the_wiring_llm(self):
        live = _plan_llm()
        child = _child_with_dead_planner()

        forked = child.fork_as_spawn(_runtime(live))

        assert forked.config.llm is live
        assert forked.config.planner is not None
        assert forked.config.planner is not child.config.planner, "must be REBOUND, not carried"

    def test_fork_as_peer_rebinds_the_planner_to_the_parent_llm(self):
        parent = Agent("investigator", system_prompt="triage")
        live = _plan_llm()
        child = _child_with_dead_planner()

        child.fork_as_peer(parent, "root-1")  # peer path must not raise

        # the explicit spawn-runtime path pins the live llm
        forked2 = child.fork_as_spawn(_runtime(live))
        assert forked2.config.planner is not None

    def test_a_forked_planner_actually_plans_against_the_live_llm(self):
        import asyncio

        live = FakeLLMAdapter(
            responses=[
                LLMResponse(
                    content=json.dumps(
                        {"steps": [{"id": "s1", "action": "noop", "depends_on": []}]}
                    ),
                    stop_reason="end_turn",
                )
            ]
        )
        from prodagent.kernel.run import Run

        child = _child_with_dead_planner()
        forked = child.fork_as_spawn(_runtime(live))

        run = Run(run_id="fork-plan-1", task="do it")
        draft = asyncio.run(forked.config.planner.generate("do it", "", [], run))
        assert len(draft.nodes) == 1
        assert live.call_count == 1, "the rebound planner must call the LIVE client"

    def test_no_planner_no_rebind_field_untouched(self):
        bare = Agent("plain", system_prompt="...")
        forked = bare.fork_as_spawn(_runtime(_plan_llm()))
        assert forked.config.planner is None
