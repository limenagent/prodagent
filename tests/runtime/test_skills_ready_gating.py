"""SKILLS_READY must only fire when the user explicitly wires a SkillRegistry.

Without ``skills=``, the event must not fire at all — printing ``SKILLS 0
runbooks:`` every turn is noise the user never opted into. With a registry
(even an empty one), the event fires because the user chose the skill path.
"""

from __future__ import annotations

import asyncio

from prodagent import Agent, AgentConfig, ExecutionMode
from prodagent.core.config import FrameworkConfig
from prodagent.kernel.bus import HookEvent, HookRegistry
from prodagent.llm.fake import script
from prodagent.skills.registry import SkillRegistry


def _run_one_turn(agent: Agent) -> None:
    asyncio.run(agent.chat("hi", session_id="skills-gating"))


def test_skills_ready_does_not_fire_without_registry():
    fired: list[dict] = []
    hooks = HookRegistry()

    def _on_skills(*, count: int = 0, names: list | None = None, **_) -> None:
        fired.append({"count": count, "names": list(names or [])})

    hooks.register_event(HookEvent.SKILLS_READY, _on_skills)

    agent = Agent(
        name="no-skills",
        system_prompt="x",
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(
            name="no-skills",
            llm=script({"content": "ok"}),
            framework=FrameworkConfig(),
            hooks=hooks,
        ),
    )
    _run_one_turn(agent)
    assert fired == []


def test_skills_ready_fires_with_empty_registry():
    fired: list[dict] = []
    hooks = HookRegistry()

    def _on_skills(*, count: int = 0, names: list | None = None, **_) -> None:
        fired.append({"count": count, "names": list(names or [])})

    hooks.register_event(HookEvent.SKILLS_READY, _on_skills)

    agent = Agent(
        name="empty-skills",
        system_prompt="x",
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(
            name="empty-skills",
            llm=script({"content": "ok"}),
            framework=FrameworkConfig(),
            skills=SkillRegistry(),
            hooks=hooks,
        ),
    )
    _run_one_turn(agent)
    assert len(fired) == 1
    assert fired[0]["count"] == 0
    assert fired[0]["names"] == []
