"""Shim — RetryPolicy moved to :mod:`prodagent.core.retry`."""

from prodagent.core.retry import Backoff, RetryPolicy

__all__ = ["RetryPolicy", "Backoff"]
