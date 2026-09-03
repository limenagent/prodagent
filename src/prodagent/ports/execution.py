"""Execution and scheduling vocabulary — who runs next, and where.

Contents: ``HandoffActivation`` describes the next hop of a peer chain (pure
data the relay returns, the driver interprets); ``Executor`` is the one
engine's contract (the Scheduler implements it); ``AgentSpec`` is the
serializable projection of an agent. The old ``AgentActivation``/``RunnerPort``
execution-location port left with the message plane — in-process activation
is just fork + drive, no port needed.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from prodagent.base.types import JsonDict
    from prodagent.kernel.budget import HardBudget
    from prodagent.kernel.types import AgentEvent

# ════════════ from runner.py ════════════


@dataclass(frozen=True)
class HandoffActivation:
    """The next hop of a peer chain, as pure data — wire-ready by construction.

    A relay decides *whether* and *where* the chain continues; interpreting
    this descriptor (peer lookup, fork, hop context) is the chain driver's
    job, so coordination never constructs runtime objects."""

    peer_name: str
    task: str
    run_id: str
    parent_run_id: str | None = None
    depth: int = 0


# ════════════ from leaf_executor.py ════════════


@runtime_checkable
class Executor(Protocol):
    """Drives one run to a terminal event — the Scheduler's contract.
    ``RunCompletedEvent`` carries the run."""

    def stream(
        self,
        task: str,
        *,
        run_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> AsyncGenerator[AgentEvent, None]: ...


# ════════════ from agent_spec.py ════════════


@dataclass
class Limits:
    """A role's hard boundaries (column 25): what a body physically can do,
    as data — not the soft guidance of a system prompt.

    - ``max_turns`` — the loop's ceiling (a budget axis);
    - ``channels`` — the state channels the role may read/write (the scope
      authorization; an empty frozenset reads as "all", a non-empty one is
      a whitelist);
    - ``can_delegate`` — the child agents the role may hand work to (an
      empty frozenset reads as "none", a non-empty one is the whitelist);
    - ``max_cost_usd`` — the spending ceiling (a budget axis).

    These are what a ``check`` gate enforces at the boundary — what you can
    reach is sturdier than what you were told to do."""

    max_turns: int = 20
    channels: frozenset[str] = frozenset()
    can_delegate: frozenset[str] = frozenset()
    max_cost_usd: float = 1.0

    def to_dict(self) -> dict[str, object]:
        return {
            "max_turns": self.max_turns,
            "channels": sorted(self.channels),
            "can_delegate": sorted(self.can_delegate),
            "max_cost_usd": self.max_cost_usd,
        }


@dataclass
class AgentSpec:
    """Pure-data agent projection — the wire form of "who runs".

    Live wiring (LLM client, hooks, stores, tool callables, registries)
    stays on ``AgentConfig``; a spec never holds a reference to any of it.

    The role (column 25) is exactly four things folded together: the soft
    instruction (``system_prompt``), the hard tool boundary (``tools_schema``
    is what the model sees; the whitelist is the composition root's), the
    outward contract (``output_model``), and the limits (``limits``)."""

    name: str
    description: str = ""
    system_prompt: str = ""
    constraints: list[str] = field(default_factory=list)
    budget: HardBudget | None = None
    tools_schema: list[JsonDict] = field(default_factory=list)
    """Tool JSON schemas (what the model sees) — not tool objects."""
    child_agents: list[AgentSpec] = field(default_factory=list)
    peers: list[AgentSpec] = field(default_factory=list)
    output_model: type[Any] | None = None
    """The role's outward contract — the structured shape its answer is
    validated against (column 25's ③)."""
    limits: Limits | None = None
    """The role's hard boundaries (channel auth, delegation whitelist,
    ceilings) — checked at the boundary, never left to the prompt."""

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
            "constraints": list(self.constraints),
            "budget": dataclasses.asdict(self.budget) if self.budget is not None else None,
            "tools_schema": [dict(s) for s in self.tools_schema],
            "child_agents": [s.to_dict() for s in self.child_agents],
            "peers": [s.to_dict() for s in self.peers],
            "output_model": (self.output_model.__name__ if self.output_model is not None else None),
            "limits": self.limits.to_dict() if self.limits is not None else None,
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
            constraints=list(d.get("constraints") or []),
            budget=HardBudget(**budget) if budget is not None else None,
            tools_schema=[dict(s) for s in d.get("tools_schema") or []],
            child_agents=[cls.from_dict(s) for s in d.get("child_agents") or []],
            peers=[cls.from_dict(s) for s in d.get("peers") or []],
            limits=(
                Limits(
                    max_turns=ld.get("max_turns", 20),
                    channels=frozenset(ld.get("channels") or []),
                    can_delegate=frozenset(ld.get("can_delegate") or []),
                    max_cost_usd=ld.get("max_cost_usd", 1.0),
                )
                if (ld := d.get("limits")) is not None
                else None
            ),
        )

    def summary(self) -> str:
        """Human-readable one-liner for logs and rosters."""
        parts = [self.name]
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
