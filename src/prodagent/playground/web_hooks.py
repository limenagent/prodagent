"""WebPushHooks — mirrors every HookEvent into an asyncio.Queue for SSE."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from prodagent.playground._json import jsonable as _jsonable

if TYPE_CHECKING:
    import asyncio

    from prodagent.kernel.bus import HookRegistry


class WebPushHooks:
    """Push every lifecycle event onto a queue as {type, event_name, **fields}."""

    def __init__(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._queue = queue

    def attach(self, hooks: HookRegistry) -> None:
        hooks.register_all_events(self.on_event)

    async def on_event(self, *, event_name: str = "", **kw: Any) -> None:
        await self._queue.put(
            {
                "type": "hook",
                "event_name": event_name,
                **_jsonable(kw),
            }
        )


__all__ = ["WebPushHooks"]
