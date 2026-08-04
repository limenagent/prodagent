"""Lifecycle events for the hook system."""

from __future__ import annotations

from enum import StrEnum


class HookEvent(StrEnum):
    SESSION_START = "session.start"
    SESSION_END = "session.end"

    LOOP_START = "loop.start"
    LOOP_END = "loop.end"

    CONTEXT_BUILD = "context.build"
    MEMORY_RECALL = "memory.recall"
    MEMORY_CLASSIFY = "memory.classify"
    INJECTION_FAILED = "injection.failed"  # injector raised — degraded, must leave a trace
    CHECKPOINT_FAILED = (
        "checkpoint.failed"  # checkpoint write raised — degraded, must leave a trace
    )

    SKILLS_READY = "skills.ready"

    TURN_START = "turn.start"
    LLM_REQUEST = "llm.request"
    THINK = "llm.think"

    TOOL_CALL = "tool.call"
    APPROVAL_REQUEST = "approval.request"
    TOOL_RESULT = "tool.result"

    PLAN_READY = "plan.ready"
    PLAN_REPLANNED = "plan.replanned"

    STEP_STARTED = "step.started"
    STEP_COMPLETED = "step.completed"
    STEP_FAILED = "step.failed"

    SKILL_LOAD = "skill.load"
    AGENT_SPAWN = "agent.spawn"
    AGENT_RESULT = "agent.result"
    PEER_HANDOFF = "peer.handoff"

    LEARNING_SYNTHESIZE = "learning.synthesize"

    TOKEN_UPDATE = "budget.token_update"
    RUN_COMPLETE = "run.complete"
    RUN_FAILED = "run.failed"


__all__ = ["HookEvent"]
