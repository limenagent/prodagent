"""Time helpers — single source of truth for "now"."""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["now_utc", "now_timestamp"]


def now_utc() -> datetime:
    """Aware UTC datetime — safe to subtract from other aware datetimes."""
    return datetime.now(UTC)


def now_timestamp() -> str:
    """ISO-8601 UTC string — the storage/display form. One format, so
    timestamps written by one code path parse in every other."""
    return now_utc().isoformat()
