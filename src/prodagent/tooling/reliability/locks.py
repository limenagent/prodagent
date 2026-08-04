from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from prodagent.core.error_reason import ErrorReason
from prodagent.core.types import ToolCall, ToolError, ToolMeta, ToolResult

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class LockRegistry:
    __slots__ = ("_semaphores",)

    def __init__(self) -> None:
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    def get_semaphore(self, resource_id: str) -> asyncio.Semaphore:
        sem = self._semaphores.get(resource_id)
        if sem is None:
            sem = asyncio.Semaphore(1)
            self._semaphores[resource_id] = sem
        return sem

    async def execute(
        self,
        call: ToolCall,
        meta: ToolMeta,
        invoke: Callable[[ToolCall], Awaitable[ToolResult]],
        agent_id: str,
        *,
        wait_timeout: float = 10.0,
    ) -> ToolResult:
        """Acquire the resource lock, then run ``invoke`` under it."""
        resource_id = meta.resource_id or ""
        sem = self.get_semaphore(resource_id)
        try:
            await asyncio.wait_for(sem.acquire(), timeout=wait_timeout)
        except TimeoutError:
            return ToolResult.from_error(
                ToolError.from_reason(
                    ErrorReason.OVERLOADED,
                    code="resource_locked",
                    message=(
                        f"Resource {meta.resource_id!r} is currently locked by another agent."
                    ),
                    hint="Try an alternative task or retry later; the lock is transient.",
                ),
                tool=call.name,
            )
        try:
            return await invoke(call)
        finally:
            sem.release()
