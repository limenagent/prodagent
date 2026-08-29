"""RetryPolicy — pluggable retry with backoff strategies."""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum


class Backoff(StrEnum):
    """Delay growth strategy between retry attempts."""

    FIXED = "fixed"
    EXPONENTIAL = "exponential"
    # Exponential with full jitter — uniform in [0, min(cap, base×2^n)].
    JITTERED = "jittered"
    # The default: without jitter, concurrent agents that failed together
    # retry together, stampeding an already-struggling upstream in lockstep.


@dataclass
class RetryPolicy:
    """Configures how the framework retries failed tool calls."""

    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    backoff: Backoff = Backoff.JITTERED

    def delay(self, attempt: int) -> float:
        """Compute sleep duration (seconds) before *attempt* (1-based)."""
        if self.backoff is Backoff.FIXED:
            return self.base_delay  # predictable cadence, no growth

        # Exponential growth clamped at max_delay — the clamp keeps a long
        # retry chain from waiting hours between attempts.
        exponential = min(self.base_delay * (2.0 ** (attempt - 1)), self.max_delay)
        if self.backoff is Backoff.JITTERED:
            # Full jitter: uniform in [0, exponential] so concurrent retryers
            # that failed together don't realign on the same retry instant.
            return random.uniform(0.0, exponential)
        return exponential
