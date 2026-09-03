"""BodyRegistry — names to node bodies, the kernel's resolution table.

Three consumers need "a name becomes a runnable unit" and they all need the
SAME table, not three private maps: the planner (a drafted node names what
it wants to run), checkpoint restore (a persisted ``run.unit_ref`` resolves
back to the live unit), and handoff lowering (an ``Outcome.control``
carrying a name crosses a run boundary). The registry holds live objects
in-process and never persists — names travel on the wire, units are
reconstructed from configuration at bind time (ruling 3).

Agents register under their own name at composition; workflows and
composed units register explicitly. A miss is loud: a draft or checkpoint
naming an unknown unit fails at resolution, never silently no-ops.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prodagent.kernel.body import BodyMeta, NodeBody

logger = logging.getLogger(__name__)

__all__ = ["BodyRegistry"]


class BodyRegistry:
    """One process's name→NodeBody table, with optional per-unit metadata."""

    def __init__(self) -> None:
        self._units: dict[str, NodeBody] = {}
        self._meta: dict[str, BodyMeta] = {}

    def register(self, name: str, unit: NodeBody, meta: BodyMeta | None = None) -> None:
        """Idempotent-by-replacement: re-registering a name wins (composition
        order is the policy — the last assembler to speak owns the name)."""
        if name in self._units and self._units[name] is not unit:
            logger.debug("unit registry: %r re-registered (new unit replaces old)", name)
        self._units[name] = unit
        if meta is not None:
            self._meta[name] = meta

    def resolve(self, name: str) -> NodeBody | None:
        return self._units.get(name)

    def require(self, name: str) -> NodeBody:
        unit = self._units.get(name)
        if unit is None:
            raise KeyError(f"unit {name!r} is not registered. Known units: {sorted(self._units)}")
        return unit

    def meta_of(self, name: str) -> BodyMeta | None:
        return self._meta.get(name)

    def names(self) -> list[str]:
        return sorted(self._units)

    def __contains__(self, name: str) -> bool:
        return name in self._units

    def __len__(self) -> int:
        return len(self._units)
