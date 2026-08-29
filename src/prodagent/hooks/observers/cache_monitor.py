"""Warn when Prompt Cache hit rate stays low past the warm-up window."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from prodagent.kernel.bus import HookEvent

if TYPE_CHECKING:
    from prodagent.kernel.bus import HookRegistry

logger = logging.getLogger(__name__)


class CacheMonitorHooks:
    """Fires a one-time warning per run when ``cache_hit_ratio`` stays below
    ``threshold`` after the warm-up turns (there's nothing to hit cache
    against on the first couple of turns, so those don't count)."""

    def __init__(self, *, threshold: float = 0.3, warmup_turns: int = 2) -> None:
        self._threshold = threshold
        self._warmup_turns = warmup_turns
        self._warned_runs: set[str] = set()

    def attach(self, hooks: HookRegistry) -> None:
        hooks.register_event(HookEvent.TOKEN_UPDATE, self._on_token_update)

    async def _on_token_update(
        self,
        *,
        turn: int = 0,
        cache_hit_ratio: float = 0.0,
        run_id: str = "",
        **_: Any,
    ) -> None:
        if turn <= self._warmup_turns:
            return  # nothing to hit cache against in the first turns — don't judge yet
        if cache_hit_ratio >= self._threshold:
            return  # healthy — clear the tracked set below stays for a fresh start
        if run_id in self._warned_runs:
            return
        self._warned_runs.add(run_id)
        logger.warning(
            "Prompt Cache hit ratio %.1f%% below threshold %.0f%% at turn %d (run=%s)",
            cache_hit_ratio * 100,
            self._threshold * 100,
            turn,
            run_id,
        )


__all__ = ["CacheMonitorHooks"]
