"""AgentSpec — the serializable projection of an agent.

``AgentConfig`` (runtime) is wiring: it holds live objects — LLM client,
hooks registry, stores, tool instances — and cannot cross a process
boundary. ``AgentSpec`` is what CAN: name, prompt, mode, budget, tool
*schemas* (not tools), and the nested specs of children/peers. A remote
spawn sends one of these; the receiving side resolves it against its roster
of live agents (unit 3+).

``Agent.spec()`` (runtime) is the projection; this module owns the round
trip. The ``budget`` field carries a :class:`~prodagent.kernel.budget.HardBudget`
— annotation-only here (ports may not import kernel at module level);
``from_dict`` reconstructs it through a function-body import, the repo's
sanctioned lazy-resolution mechanism.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from prodagent.base.types import ExecutionMode, JsonDict

if TYPE_CHECKING:
    from prodagent.kernel.budget import HardBudget

__all__ = ["AgentSpec"]


@dataclass
class AgentSpec:
    """Pure-data agent projection — the wire form of "who runs".

    Live wiring (LLM client, hooks, stores, tool callables, registries)
    stays on ``AgentConfig``; a spec never holds a reference to any of it."""

    name: str
    description: str = ""
    system_prompt: str = ""
    mode: ExecutionMode = ExecutionMode.REACTIVE
    constraints: list[str] = field(default_factory=list)
    budget: HardBudget | None = None
    tools_schema: list[JsonDict] = field(default_factory=list)
    """Tool JSON schemas (what the model sees) — not tool objects."""
    max_replans: int = 2
    child_agents: list[AgentSpec] = field(default_factory=list)
    peers: list[AgentSpec] = field(default_factory=list)

    def describe(self) -> str:
        """One-line description: prefer ``description``, fall back to a
        truncated system prompt — the roster/tool-schema blurb."""
        if self.description:
            return self.description
        if self.system_prompt:
            prompt = self.system_prompt[:80]
            return prompt + "..." if len(self.system_prompt) > 80 else prompt
        return ""

    def to_dict(self) -> JsonDict:
        """Hand-written (curated, not codec-dumped): the wire projection a
        remote roster resolves. Nested specs recurse; budget is a plain dict."""
        return {
            "name": self.name,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "mode": self.mode.value,
            "constraints": list(self.constraints),
            "budget": dataclasses.asdict(self.budget) if self.budget is not None else None,
            "tools_schema": [dict(s) for s in self.tools_schema],
            "max_replans": self.max_replans,
            "child_agents": [s.to_dict() for s in self.child_agents],
            "peers": [s.to_dict() for s in self.peers],
        }

    @classmethod
    def from_dict(cls, d: JsonDict) -> AgentSpec:
        """Inverse of ``to_dict`` — the receiving side of a remote spawn."""
        from prodagent.kernel.budget import HardBudget

        budget = d.get("budget")
        return cls(
            name=d["name"],
            description=d.get("description", ""),
            system_prompt=d.get("system_prompt", ""),
            mode=ExecutionMode(d.get("mode", ExecutionMode.REACTIVE.value)),
            constraints=list(d.get("constraints") or []),
            budget=HardBudget(**budget) if budget is not None else None,
            tools_schema=[dict(s) for s in d.get("tools_schema") or []],
            max_replans=d.get("max_replans", 2),
            child_agents=[cls.from_dict(s) for s in d.get("child_agents") or []],
            peers=[cls.from_dict(s) for s in d.get("peers") or []],
        )

    def summary(self) -> str:
        """Human-readable one-liner for logs and rosters."""
        parts = [self.name, self.mode.value]
        if self.child_agents:
            parts.append(f"children={len(self.child_agents)}")
        if self.peers:
            parts.append(f"peers={len(self.peers)}")
        return " ".join(parts)


def spec_from_any(obj: Any) -> AgentSpec:
    """Spec of anything spec-shaped: an AgentSpec as-is, or any object with a
    ``spec()`` projection (the live Agent)."""
    if isinstance(obj, AgentSpec):
        return obj  # already the wire form — no projection needed
    spec = getattr(obj, "spec", None)
    if callable(spec):
        return cast("AgentSpec", spec())  # a live Agent projects itself
    raise TypeError(f"cannot derive an AgentSpec from {obj!r}")  # neither — wiring bug
