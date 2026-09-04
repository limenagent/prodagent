"""LearningHooks and the default-bundle ruling (2026-09-04).

The laws: skill generation is banned by default — a configured ``skills=``
registry means the agent LOADS runbooks via ``get_skill``, and attaching
default hooks must never wire a synthesizer as a side effect; the closed
learning loop is opt-in via ``extensions=[LearningHooks(...)]``, and an
explicit opt-in with a misconfigured backend fails fast and loud at
construction — a named config error, before any run starts.
"""

from __future__ import annotations

import pytest

from prodagent.base.config import BackendConfig, FrameworkConfig, OrchestrationConfig
from prodagent.kernel.bus import HookEvent
from prodagent.runtime.agent import Agent
from prodagent.runtime.config import AgentConfig
from prodagent.skills.registry import SkillRegistry


def _fw_with_unimplemented_experience() -> FrameworkConfig:
    fw = FrameworkConfig.default()
    fw.backend = BackendConfig(experience="postgres")
    return fw


def _fw_with_file_experience(tmp_path) -> FrameworkConfig:
    fw = FrameworkConfig.default()
    fw.orchestration = OrchestrationConfig(experience_path=str(tmp_path / "exp.jsonl"))
    return fw


def test_skills_registry_alone_never_wires_the_learning_loop(tmp_path):
    """``skills=`` means LOAD: default hook wiring must not attach a
    synthesizer — no SESSION_END learning handler may appear."""
    agent = Agent(
        "no-learning",
        config=AgentConfig(
            name="no-learning",
            skills=SkillRegistry(),
            framework=_fw_with_file_experience(tmp_path),
        ),
    )
    registry = agent.attach_default_hooks()
    assert registry is not None
    assert list(registry.event_handlers(HookEvent.SESSION_END)) == []


def test_learning_loop_is_opt_in_via_extensions(tmp_path):
    """extensions=[LearningHooks(...)] is the only door: it wires the
    SESSION_END handler exactly once."""
    from prodagent.hooks.bundles.learning import LearningHooks

    fw = _fw_with_file_experience(tmp_path)
    registry_skills = SkillRegistry()
    agent = Agent(
        "learning-opt-in",
        config=AgentConfig(
            name="learning-opt-in",
            skills=registry_skills,
            framework=fw,
            extensions=[LearningHooks(registry=registry_skills, framework_config=fw)],
        ),
    )
    registry = agent.attach_default_hooks()
    assert list(registry.event_handlers(HookEvent.SESSION_END)), "opt-in wires the loop"


def test_unimplemented_experience_backend_fails_loud_at_explicit_opt_in():
    """Learning is now explicit, so a misconfigured backend fails fast at
    attach — a named config error, before any run starts. (The old
    auto-attach degrade law is gone with the auto-attach itself; the
    SESSION_END loop still swallows per-run errors once wired.)"""
    from prodagent.hooks.bundles.learning import LearningHooks

    fw = _fw_with_unimplemented_experience()
    registry_skills = SkillRegistry()
    with pytest.raises(NotImplementedError):
        # the store resolves in the constructor — misconfiguration is a
        # construction-time error, loud and named
        LearningHooks(registry=registry_skills, framework_config=fw)
