"""Time helpers — single source of truth for "now".

Both forms draw from the determinism port (``ports.determinism``), so a
replay's frozen clock governs datetimes exactly as it governs epoch floats —
there is no second clock to forget to substitute.
"""

from __future__ import annotations

from datetime import UTC, datetime

from prodagent.base.determinism import now_wall

__all__ = ["now_utc", "now_timestamp"]


def now_utc() -> datetime:
    """Aware UTC datetime — safe to subtract from other aware datetimes."""
    return datetime.fromtimestamp(now_wall(), UTC)


def now_timestamp() -> str:
    """ISO-8601 UTC string — the storage/display form. One format, so
    timestamps written by one code path parse in every other."""
    return now_utc().isoformat()
