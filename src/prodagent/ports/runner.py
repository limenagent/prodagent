"""RunnerPort — the execution-position seam: activate an agent, anywhere.

"Execute an agent run" used to be three concrete calls welded to the
in-process runtime (spawn's ``drive()`` a coordination module lazy-imported,
stage members' ``agent.chat()``, the relay handing back a ``RunContext``).
This port makes it a contract instead: describe *one activation* — who runs,
what task, under which identity — and consume the event stream. Where the
agent actually executes (this process, another machine) is the
implementation's business.

Vocabulary relationships:

- input :class:`AgentActivation` is the single-unit form of
  :mod:`prodagent.ports.activation`'s ``Activation`` batch — the stage driver
  dispatches a batch, the runner executes each member;
- input :class:`HandoffActivation` is what a peer relay returns: a pure-data
  description of the next hop, interpreted by whoever drives the chain;
- output is the kernel's ``AgentEvent`` stream (``ports.leaf_executor`` is the
  precedent for a port typing itself against kernel events — they become the
  wire vocabulary proper in the data-model unit).

:class:`InProcessChatRunner` is the default local implementation for
session-scoped member turns, provided beside the port (same precedent as the
messaging plane's in-process Transport beside ``ports.Transport``); the full
hop runner — fork + drive + ledger join — is ``runtime.runner``'s
``InProcessRunner``, which stays with the runtime it wraps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from prodagent.kernel.types import AgentEvent
    from prodagent.ports.budget_ledger import BudgetLedgerPort

__all__ = [
    "AgentActivation",
    "HandoffActivation",
    "RunnerPort",
    "InProcessChatRunner",
]


@dataclass(frozen=True)
class AgentActivation:
    """One agent activation — the unit of execution at the RunnerPort.

    ``agent`` is an Agent object in-process; a distributed runtime passes a
    name (the control plane resolves it). Two bindings, selected by which
    optional field is set:

    - ``session_id`` set — a session-scoped member turn (the session allocates
      the run identity; ensemble/blackboard members). Never forked.
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
