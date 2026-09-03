"""handoff — the one control-transfer word, from kernel vocabulary to wire.

Control transfer already has exactly one mechanism: a run parks a
``PendingHandoff``, completes, and the relay mints a pure-data
``HandoffActivation`` the chain driver interprets. What this module adds
is the *vocabulary* bridge — the same transfer expressed as the kernel's
``Outcome(control=Handoff(target, carry))`` (what a Unit returns, what
combinators propagate) — and the lowering rule that keeps the two worlds
from mixing:

- In-process, ``Handoff.target`` is a live Unit reference.
- Crossing a run or process boundary, the target becomes a *name* —
  resolved against the UnitRegistry (or the agent roster) — and travels
  as ``HandoffActivation`` (ruling 3: names on the wire, never objects).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from prodagent.kernel.unit import Handoff, Outcome

if TYPE_CHECKING:
    from prodagent.kernel.registry import UnitRegistry
    from prodagent.kernel.run import PendingHandoff
    from prodagent.kernel.unit import GraphUnit
    from prodagent.ports.execution import HandoffActivation

logger = logging.getLogger(__name__)

__all__ = ["handoff", "handoff_of", "lower_to_activation"]


def handoff(
    target: GraphUnit | str,
    *,
    task: str,
    input_refs: dict[str, str] | None = None,
    carry: str = "full",
) -> Outcome:
    """The control-transfer Outcome a Unit returns to say "the rest is
    someone else's": control really moves, this run does not continue.

    A live Unit reference stays a reference (in-process); a string is kept
    as the name (the registry resolves it at the boundary). Either way the
    Outcome is the same kernel word — callers never guess which semantics
    they got."""
    return Outcome(control=Handoff(target=target, carry=carry))  # type: ignore[arg-type]


def handoff_of(outcome: Outcome) -> Handoff | None:
    """The Handoff control an outcome carries, if any — the read side of
    :func:`handoff` for drivers folding outcomes."""
    return outcome.control if isinstance(outcome.control, Handoff) else None


def lower_to_activation(
    pending: PendingHandoff, registry: UnitRegistry | None = None
) -> HandoffActivation:
    """The wire form of a parked handoff: peer name, task, identity — pure
    data, resolvable on any machine. This is the relay's existing output;
    the module states it once so the vocabulary and the wire never drift."""
    from prodagent.ports.execution import HandoffActivation

    return HandoffActivation(
        peer_name=pending.peer_name,
        task=pending.task or pending.prior_output,
        run_id=pending.peer_run_id or "",
        parent_run_id=None,
        depth=0,
    )


def target_name(target: GraphUnit | str, registry: UnitRegistry | None = None) -> str:
    """The wire name for a Handoff target: a string stays; a live Unit maps
    through the registry (its roster name) — an unregistered live target
    cannot cross and says so."""
    if isinstance(target, str):
        return target
    name = getattr(target, "name", None) or getattr(target, "target", "") or ""
    if not isinstance(name, str) or not name:
        raise ValueError(
            "handoff target carries no name and cannot cross a run boundary — "
            "register it in the UnitRegistry or hand off by name"
        )
    return str(name)
