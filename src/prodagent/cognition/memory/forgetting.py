"""Forgetting — activation as decay × frequency × recency.

Reversible, not scheduled deletion: nothing here removes a memory. Each
memory earns an activation score (ACT-R-shaped: exponential decay over its
TTL, boosted by how often and how recently it was used); recall filters on
the score against ``RECALL_FLOOR``. A memory that keeps proving useful
outlives its decay curve; one that stops being touched fades from recall
while its record stays — resurrectable by the next touch. Constraints and
facts never decay: they are load-bearing, not impressions.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prodagent.cognition.memory.storage import StoredMemory

__all__ = ["activation", "RECALL_FLOOR"]


RECALL_FLOOR = 0.05
_FREQ_WEIGHT = 0.2
_RECENCY_WINDOW_DAYS = 7
_RECENCY_BOOST = 0.3


def _frequency(access_count: int) -> float:
    return math.log(1 + max(0, access_count)) * _FREQ_WEIGHT


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return _as_utc(parsed)


def _recency(mem: StoredMemory, now: datetime) -> float:
    if not mem.last_access:
        return 0.0
    last = _parse_utc(mem.last_access)
    if last is None:
        return 0.0
    return _RECENCY_BOOST if (_as_utc(now) - last).days < _RECENCY_WINDOW_DAYS else 0.0


def activation(mem: StoredMemory, now: datetime) -> float:
    """Activation in ``[0.0, 1.0]``. Higher = more activated.

    Classic memory-activation shape (ACT-R lineage): exponential base decay
    with TTL, plus frequency and recency boosts — a memory can outlive its
    decay curve by staying useful."""

    if mem.memory_type in ("constraint", "fact"):
        return 1.0  # load-bearing knowledge never decays — only impressions do
    if mem.ttl_days is None:
        return 1.0  # no TTL declared = immortal by choice

    created = _parse_utc(mem.created_at)
    if created is None:
        return 1.0  # unparseable timestamp: keep the memory rather than drop it silently
    age_days = max((_as_utc(now) - created).days, 0)
    # Exponential decay over the TTL: ~37% activation at one TTL, ~13% at two.
    base = math.exp(-age_days / max(int(mem.ttl_days), 1))
    # Boosts can rescue a decaying memory: usefulness outruns age.
    return min(1.0, base + _frequency(mem.access_count) + _recency(mem, now))
