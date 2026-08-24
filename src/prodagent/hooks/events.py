"""Shim — the event vocabulary moved to :mod:`prodagent.kernel.bus`."""

from prodagent.kernel.bus import HookEvent

__all__ = ["HookEvent"]
