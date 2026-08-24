"""Shim — gate vocabulary moved to :mod:`prodagent.kernel.bus`."""

from prodagent.kernel.bus import BlockingResult, FailurePolicy, Gate, InjectionPoint

__all__ = ["BlockingResult", "FailurePolicy", "Gate", "InjectionPoint"]
