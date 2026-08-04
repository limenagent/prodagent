from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from prodagent.core.types import ToolName

logger = logging.getLogger(__name__)


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class _ToolBreakerState:
    failures: deque[float] = field(default_factory=deque)
    state: BreakerState = BreakerState.CLOSED
    probe_in_flight: bool = False

    def evict(self, window: float) -> None:
        cutoff = time.monotonic() - window
        while self.failures and self.failures[0] < cutoff:
            self.failures.popleft()


class ToolCircuitBreaker:
    # Failures stored as monotonic timestamps; counts reflect only failures inside window_seconds.
    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 60.0,
        *,
        window_seconds: float = 300.0,
        window_size: int = 10_000,
    ) -> None:
        self._threshold = failure_threshold
        self._recovery = recovery_timeout_seconds
        self._window = window_seconds
        self._window_size = window_size
        self._states: dict[ToolName, _ToolBreakerState] = {}
        self._locks: dict[ToolName, asyncio.Lock] = {}

    def _get_lock(self, name: ToolName) -> asyncio.Lock:
        if name not in self._locks:
            self._locks[name] = asyncio.Lock()
        return self._locks[name]

    def _state(self, name: ToolName) -> _ToolBreakerState:
        if name not in self._states:
            self._states[name] = _ToolBreakerState(failures=deque(maxlen=self._window_size))
        return self._states[name]

    def _maybe_transition_half_open(self, s: _ToolBreakerState, name: ToolName) -> bool:
        last = s.failures[-1] if s.failures else 0.0
        if time.monotonic() - last >= self._recovery:
            s.state = BreakerState.HALF_OPEN
            logger.info("ToolBreaker[%s]: HALF_OPEN — probe request allowed", name)
            return True
        return False

    async def is_available(self, name: ToolName) -> bool:
        async with self._get_lock(name):
            s = self._state(name)
            if s.state is BreakerState.OPEN:
                last = s.failures[-1] if s.failures else 0.0
                return time.monotonic() - last >= self._recovery
            return True

    async def try_acquire_probe(self, name: ToolName) -> bool:
        async with self._get_lock(name):
            s = self._state(name)
            if s.state is BreakerState.CLOSED:
                return True
            if s.state is BreakerState.OPEN and not self._maybe_transition_half_open(s, name):
                return False
            # HALF_OPEN: only one probe in flight
            if s.probe_in_flight:
                return False
            s.probe_in_flight = True
            return True

    async def release_probe(self, name: ToolName) -> None:
        async with self._get_lock(name):
            self._state(name).probe_in_flight = False

    async def record_success(self, name: ToolName) -> None:
        async with self._get_lock(name):
            s = self._state(name)
            if s.state is not BreakerState.CLOSED:
                logger.info("ToolBreaker[%s]: CLOSED (recovered)", name)
            s.failures.clear()
            s.state = BreakerState.CLOSED
            s.probe_in_flight = False

    async def record_failure(self, name: ToolName) -> None:
        async with self._get_lock(name):
            s = self._state(name)
            s.failures.append(time.monotonic())
            s.probe_in_flight = False

            if s.state is BreakerState.HALF_OPEN:
                s.state = BreakerState.OPEN
                logger.warning("ToolBreaker[%s]: probe failed → OPEN", name)
                return

            s.evict(self._window)
            if len(s.failures) >= self._threshold and s.state is BreakerState.CLOSED:
                s.state = BreakerState.OPEN
                logger.warning(
                    "ToolBreaker[%s]: OPEN after %d failures in %.0fs window",
                    name,
                    len(s.failures),
                    self._window,
                )

    async def status(self, name: ToolName) -> dict[str, Any]:
        async with self._get_lock(name):
            s = self._state(name)
            s.evict(self._window)
            last = s.failures[-1] if s.failures else None
            return {
                "tool": name,
                "state": s.state.value,
                "failures": len(s.failures),
                "seconds_since_last_failure": (
                    round(time.monotonic() - last, 1) if last is not None else None
                ),
            }
