"""TouchBackWorker — recall reinforcement, off the recall path.

Remembering something should strengthen it (the activation curve feeds on
access counts), but a store write on the hot path would make recall pay
for its own bookkeeping. This queue decouples them: recall enqueues the
id and moves on; a background task drains touches serially. Nothing is
lost if the process dies in between — a missed touch only means one
recall didn't count, never a wrong answer."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prodagent.ports.document import DocumentStore

logger = logging.getLogger(__name__)


class TouchBackWorker:
    """Serial touch-back queue — retrieval reinforcement without blocking recall."""

    def __init__(self, store: DocumentStore) -> None:
        self._store = store
        self._queue: asyncio.Queue[str] | None = None
        self._task: asyncio.Task[None] | None = None

    def enqueue(self, mem_id: str) -> None:
        """Queue a touch — fire-and-forget from the recall path's view; the
        background worker drains it at its own pace. Reinforcement must never
        sit on the critical path of remembering."""
        queue = self._get_queue()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
        queue.put_nowait(mem_id)

    def _get_queue(self) -> asyncio.Queue[str]:
        if self._queue is None:
            self._queue = asyncio.Queue()
        return self._queue

    async def _run(self) -> None:
        """Drain loop — one touch at a time (serialization keeps the store
        orderly); a failed touch logs and moves on, never kills the worker."""
        queue = self._get_queue()
        while True:
            mem_id = await queue.get()
            try:
                await self._store.touch_memory(mem_id)
            except Exception as exc:
                logger.warning("[memory] touch_memory failed for %s: %s", mem_id, exc)
            finally:
                queue.task_done()

    async def aclose(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None
