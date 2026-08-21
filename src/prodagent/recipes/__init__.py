"""One-line assemblies of production mechanisms (recipes).

A recipe is a curated default stack — the same primitives the tour pages
walk through, pre-composed for the most common agent shapes. Recipes are
thin: each one is an ``Agent`` with a tuned ``AgentConfig``, nothing the
power user can't rebuild (and then diverge from) via the two-tier
constructor. Start from the whole vehicle, then swap parts.
"""

from prodagent.recipes.presets import audit_agent, delegation_agent, research_agent

__all__ = ["audit_agent", "research_agent", "delegation_agent"]
