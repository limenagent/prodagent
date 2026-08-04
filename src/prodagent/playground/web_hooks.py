"""WebPushHooks — mirrors every HookEvent into an asyncio.Queue for SSE."""

from __future__ import annotations

import dataclasses
import enum
import logging
from pathlib import PurePath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncio

    from prodagent.hooks.registry import HookRegistry

logger = logging.getLogger(__name__)


def _jsonable(obj: Any) -> Any:
    """Recursively coerce *obj* to JSON-serializable primitives."""
    if obj is None or isinstance(obj, bool | int | float | str):
        return obj
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, PurePath):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set | frozenset):
        return [_jsonable(v) for v in obj]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if hasattr(obj, "model_dump") and callable(obj.model_dump):
        try:
            return _jsonable(obj.model_dump(mode="json"))
        except Exception:
            logger.warning(
                "[web_hooks] model_dump() failed for %r; falling back to repr", type(obj).__name__
            )
    return repr(obj)


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
