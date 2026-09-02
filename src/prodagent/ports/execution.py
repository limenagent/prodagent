"""Execution and scheduling vocabulary — who runs next, and where.

Family home for the book's model-and-execution socket family plus its internal
contracts (four modules merged 2026-08: activation / runner / leaf_executor
/ agent_spec, whose docstrings cite each other — ``AgentActivation`` is the
single-unit form of ``Activation``'s batch, ``Executor`` is the cited
precedent for a port typing itself against kernel events, ``AgentSpec`` is
what a distributed runner resolves agent names against).

Contents, in dependency order: the activation vocabulary (``DispatchMode``,
``StageStore``, ``Activation``, ``ActivationContext``, ``ActivationPolicy``)
decides who acts next; ``RunnerPort`` + the two activation descriptors decide
where that activation executes; ``Executor`` is the one engine's contract
(the Scheduler implements it); ``AgentSpec`` is the serializable projection
of an agent.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, runtime_checkable

from prodagent.base.types import ExecutionMode, JsonDict

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable

    from prodagent.kernel.budget import HardBudget
    from prodagent.kernel.types import AgentEvent
    from prodagent.ports.budget_ledger import BudgetLedgerPort

# ════════════ from activation.py ════════════

DispatchMode = Literal["serial", "concurrent", "single_winner"]
"""How an activation's members run:

- ``serial`` — one at a time, in member order (round-robin floor, moderated pick).
- ``concurrent`` — all at once, results collected together (event-mode trigger
  fan-out, a work-queue round's claim race).
- ``single_winner`` — all *race*, but only one computes (buzz_in: the first to
  grab the lock wins; losers must never start real work).
"""


@runtime_checkable
class StageStore(Protocol):
    """Read-side contract of a stage's shared store, as an activation policy
    sees it. Satisfied structurally by coordination's ``SharedStore`` family;
    policies that need a concrete store's specifics down-cast in their own
    layer."""

    def round_count(self) -> int: ...

    def snapshot(self) -> dict[str, Any]: ...

    def fingerprint(self) -> Any: ...


@dataclass(frozen=True)
class Activation:
    """One scheduled batch of member activations.

    ``round_num`` is the round this batch belongs to — computed by the policy,
    because "when does a round advance" is order-specific (round-robin wraps by
    member position; a free-for-all advances every batch; a trigger board
    advances every drain cycle).
    """

    members: list[str]
    dispatch: DispatchMode = "serial"
    round_num: int = 0
    label: str = ""
    """Why this activation exists — trigger name, order name, "pull". For logs/events."""

    def why(self) -> str:
        return self.label or ",".join(self.members)


@dataclass(frozen=True)
class ActivationContext:
    """What an ActivationPolicy sees when deciding the next activation(s).

    ``store`` is the live shared store (floor / board / queue — read-only from
    the policy's perspective). ``changed_keys`` is what mutated last round:
    the board's drained change list; empty/None for stores whose writes aren't
    key-shaped (transcripts, queue transitions).
    """

    store: StageStore
    changed_keys: tuple[str, ...] = ()
    round_num: int = 0
    """The round the *next* activation would run in (same convention as
    ``TerminationStrategy.should_stop(next_round=...)``)."""


@runtime_checkable
class ActivationPolicy(Protocol):
    """Decides who acts next. Returns one or more :class:`Activation` batches
    for the coming round, or an empty list when there is no pending work —
    which the driver surfaces as its quiescent/no-activation stop reason.

    Async by design: a moderated picker may await an LLM to name the next
    speaker."""

    def next_activations(self, ctx: ActivationContext) -> Awaitable[list[Activation]]: ...


# ════════════ from runner.py ════════════


@dataclass(frozen=True)
class AgentActivation:
    """One agent activation — the unit of execution at the RunnerPort.

    ``agent`` is an Agent object in-process; a distributed runtime passes a
    name (the control plane resolves it). Two bindings, selected by which
    optional field is set:

    - ``session_id`` set — a session-scoped member turn (the session allocates
      the run identity; stage members). Never forked.
    - otherwise — a bare run under the given identity (spawn children, graph
      nodes), joined to ``budget_ledger`` when one is supplied. An
      implementation bound to a hop's wiring forks the target under it
      (child-of-chain semantics); an unbound one runs the target as-is.
    """

    agent: Any
    task: str
    run_id: str | None = None
    parent_run_id: str | None = None
    depth: int = 0
    session_id: str | None = None
    budget_ledger: BudgetLedgerPort | None = None


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


@runtime_checkable
class RunnerPort(Protocol):
    """Activate one agent per the descriptor; the terminal event carries the run."""

    def activate(self, activation: AgentActivation) -> AsyncGenerator[AgentEvent, None]: ...


class InProcessChatRunner:
    """The local default for session-scoped member turns: stream the agent's
    own chat loop. No fork, no ledger — the stage's envelope owns budgeting
    around the turn."""

    def activate(self, activation: AgentActivation) -> AsyncGenerator[AgentEvent, None]:
        if activation.session_id is None:
            # The port accepts bare activations too; this implementation is
            # session-scoped by design — misrouting here would fork a member
            # that should speak as itself.
            raise ValueError(
                "InProcessChatRunner activates session-scoped member turns — "
                f"pass session_id (agent={activation.agent!r})"
            )
        # ``agent`` is Any by design (a name on the wire in a distributed
        # runtime); in-process it is an Agent whose chat_stream yields AgentEvents.
        return cast(
            "AsyncGenerator[AgentEvent, None]",
            activation.agent.chat_stream(activation.task, session_id=activation.session_id),
        )


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
