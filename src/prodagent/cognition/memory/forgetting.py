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
    """Activation in ``[0.0, 1.0]``. Higher = more activated."""
    if mem.memory_type in ("constraint", "fact"):
        return 1.0
    if mem.ttl_days is None:
        return 1.0

    created = _parse_utc(mem.created_at)
    if created is None:
        return 1.0
    age_days = max((_as_utc(now) - created).days, 0)
    base = math.exp(-age_days / max(int(mem.ttl_days), 1))
    return min(1.0, base + _frequency(mem.access_count) + _recency(mem, now))
