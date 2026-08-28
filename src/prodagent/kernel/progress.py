"""Progress monitor — dead-loop and ghost-loop detection."""

from __future__ import annotations

import collections
import hashlib
import json
import logging
from typing import TYPE_CHECKING

from prodagent.base.errors import InfiniteLoopDetected
from prodagent.base.types import stable_serialize

if TYPE_CHECKING:
    from prodagent.kernel.state import AgentRun
    from prodagent.kernel.types import ToolCall

logger = logging.getLogger(__name__)

DEFAULT_FINGERPRINT_WINDOW = 5
DEFAULT_STALL_THRESHOLD = 4
DEFAULT_REPEAT_THRESHOLD = 5


_LIMIT_ONLY_KEYS = frozenset({"limit"})


def _tool_fingerprint(call: ToolCall) -> str:
    # A call that differs only in ``limit`` is still the same call — degenerate
    # paging (same query, drifting limit) must count as a loop.
    params = {k: v for k, v in call.params.items() if k not in _LIMIT_ONLY_KEYS}
    payload = json.dumps(
        {"name": call.name, "params": params},
        sort_keys=True,
        default=stable_serialize,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


def _context_hash(run: AgentRun) -> str:
    recent = run.messages[-6:] if len(run.messages) >= 6 else run.messages
    payload = json.dumps(recent, default=stable_serialize)
    return hashlib.md5(payload.encode()).hexdigest()


class ProgressMonitor:
    """Detect dead loops and ghost loops for one run.

    The dead-loop window lives on the run (``AgentRun.fingerprints``) so it is
    checkpointed: a resumed run keeps its loop memory instead of re-tripping
    the same loop from a zeroed counter."""

    def __init__(
        self,
        *,
        repeat_threshold: int = DEFAULT_REPEAT_THRESHOLD,
        window_size: int = DEFAULT_FINGERPRINT_WINDOW,
        stall_threshold: int = DEFAULT_STALL_THRESHOLD,
    ) -> None:
        self._repeat_threshold = repeat_threshold
        self._window_size = window_size
        self._stall_threshold = stall_threshold
        self._hashes: collections.deque[str] = collections.deque(maxlen=stall_threshold)

    def check(self, run: AgentRun, new_call: ToolCall | None = None) -> None:
        if new_call is not None:
            self._check_dead_loop(run, new_call)
        else:
            self._check_ghost_loop(run)

    def _check_dead_loop(self, run: AgentRun, new_call: ToolCall) -> None:
        fp = _tool_fingerprint(new_call)
        count = run.push_fingerprint(fp, window=self._window_size)

        if count >= self._repeat_threshold:
            raise InfiniteLoopDetected(
                f"Dead loop: tool '{new_call.name}' with identical params "
                f"called {count} times within the last {self._window_size} calls "
                f"(threshold={self._repeat_threshold})",
                run_id=run.run_id,
                tool=new_call.name,
            )

    def _check_ghost_loop(self, run: AgentRun) -> None:
        # Ghost loop: turns keep happening but the recent context stops
        # changing — work with no progress, the quieter sibling of a dead loop.
        current = _context_hash(run)
        self._hashes.append(current)

        if len(self._hashes) >= self._stall_threshold and len(set(self._hashes)) == 1:
            raise InfiniteLoopDetected(
                f"Ghost loop: context hash unchanged for {self._stall_threshold} consecutive turns",
                run_id=run.run_id,
                hash_value=current,
            )
