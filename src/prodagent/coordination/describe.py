"""Describe an agent for a tool schema — duck-typed, layer-neutral.

Spawn/peer tools list the available sub-agents and peers in their
descriptions; this projection reads ``config.description`` (falling back to a
truncated system prompt) off any agent-shaped object. In the data-model unit
this becomes a projection off the serializable AgentSpec; today it accepts
the live Agent structurally, which keeps coordination free of runtime types.
"""

from __future__ import annotations

from typing import Any

__all__ = ["describe_agent"]


def describe_agent(a: Any) -> str:
    """Tool-schema description: prefer ``description``, fall back to truncated system prompt."""
    config = getattr(a, "config", None)
    description = getattr(config, "description", "") if config is not None else ""
    if description:
        return description
    system_prompt = getattr(config, "system_prompt", "") if config is not None else ""
    if system_prompt:
        prompt = system_prompt[:80]
        return prompt + "..." if len(system_prompt) > 80 else prompt
    return ""
