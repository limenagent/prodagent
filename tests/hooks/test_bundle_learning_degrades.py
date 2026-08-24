"""LearningHooks must not crash attach_default_hooks when the configured
experience backend isn't implemented yet.

Repro: PRODAGENT_BACKEND=prod flips backend.experience to 'postgres', but
resolve_experience only implements the 'file' branch. The learning bundle
is a best-effort side-channel (its own _safely_run_loop already swallows
exceptions) — an unimplemented journal backend must degrade to "no
learning loop" rather than crash the run before it starts.
"""

from __future__ import annotations

from prodagent.core.config import BackendConfig, FrameworkConfig, OrchestrationConfig
from prodagent.kernel.bus import HookEvent
from prodagent.runtime.agent import Agent
from prodagent.runtime.config import AgentConfig
from prodagent.skills.registry import SkillRegistry


def _fw_with_unimplemented_experience() -> FrameworkConfig:
    fw = FrameworkConfig.default()
    fw.backend = BackendConfig(experience="postgres")
    return fw


def test_attach_default_hooks_survives_unimplemented_experience_backend():
    agent = Agent(
        "learning-degrade",
        config=AgentConfig(
            name="learning-degrade",
            skills=SkillRegistry(),
            framework=_fw_with_unimplemented_experience(),
        ),
    )
    # Must not raise NotImplementedError.
    registry = agent.attach_default_hooks()
    assert registry is not None


def test_attach_default_hooks_with_file_experience_still_wires_learning(tmp_path):
    """Sanity: when the backend IS implemented, LearningHooks still attaches."""
    fw = FrameworkConfig.default()
    fw.orchestration = OrchestrationConfig(experience_path=str(tmp_path / "exp.jsonl"))
    agent = Agent(
        "learning-normal",
        config=AgentConfig(
            name="learning-normal",
            skills=SkillRegistry(),
            framework=fw,
        ),
    )
    registry = agent.attach_default_hooks()
    handlers = list(registry.event_handlers(HookEvent.SESSION_END))
    # LearningHooks registers a SESSION_END handler — at least one handler
    # beyond the base bundles should be present.
    assert len(handlers) >= 1
