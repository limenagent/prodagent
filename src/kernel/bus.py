"""bus — the kernel's single seam to the outside world.

Two usage styles for two kinds of consumers:

1. Cross-cutting plugins use the "three callback protocols":
   - fire: observe. Observability, metering, and audit attach here. An
     observer's error never affects the main flow.
   - check: adjudicate. Approval, permission, and budget gates attach here;
     any one veto blocks. Safe default is fail-closed: if a checker itself
     raises, that counts as a veto, never as approval.
   - collect: gather. When assembling a prompt, collect extra fragments each
     plugin wants to inject.

2. Streaming consumers use "subscription queue + backpressure":
   subscribe() returns a bounded queue that receives high-frequency events
   such as tokens. When the queue is full there are two policies —
   "block" makes the producer wait at the delivery point, pushing pressure all
   the way back to the model stream;
   "drop" never blocks, drops frames when full and counts them, suitable for
   progress displays that tolerate missing frames.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

Handler = Callable[..., Any]


@dataclass
class BlockingResult:
    allowed: bool
    reason: str = ""


class Subscription:
    """A bounded event subscription: await get() or ``async for`` the events."""

    def __init__(self, kinds: tuple[str, ...], maxsize: int, on_full: str):
        self.kinds = kinds  # empty tuple means "subscribe to all"
        self.on_full = on_full
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0  # frames dropped under the drop policy
        self._closed = False
        self._on_close = None  # injected by Bus: remove itself on close

    def _wants(self, event: str) -> bool:
        return not self.kinds or event in self.kinds

    async def _deliver(self, event: str, data: dict) -> None:
        if not self._wants(event) or self._closed:
            return
        item = {"event": event, **data}
        if self.on_full == "block":
            await self.queue.put(item)  # wait here when full → backpressure upstream
            return
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self.dropped += 1  # drop: never block, just account for it

    async def get(self) -> dict:
        return await self.queue.get()

    def close(self) -> None:
        self._closed = True
        if self._on_close is not None:  # unregister from the bus so subscriptions don't pile up
            self._on_close()
            self._on_close = None

    async def __anext__(self) -> dict:
        if self._closed and self.queue.empty():
            raise StopAsyncIteration
        return await self.queue.get()

    def __aiter__(self) -> Subscription:
        return self


class Bus:
    def __init__(self) -> None:
        self._observers: dict[str, list[Handler]] = {}
        self._checkers: dict[str, list[Handler]] = {}
        self._providers: dict[str, list[Handler]] = {}
        self._subscriptions: list[Subscription] = []

    # — callback registration —
    def on(self, event: str, handler: Handler) -> None:
        self._observers.setdefault(event, []).append(handler)

    def checker(self, gate: str, handler: Handler) -> None:
        self._checkers.setdefault(gate, []).append(handler)

    def provider(self, point: str, handler: Handler) -> None:
        self._providers.setdefault(point, []).append(handler)

    # — streaming subscription (backpressure) —
    def subscribe(self, *kinds: str, maxsize: int = 0, on_full: str = "block") -> Subscription:
        if on_full not in ("block", "drop"):
            raise ValueError("on_full must be 'block' or 'drop'")
        sub = Subscription(kinds, maxsize, on_full)
        self._subscriptions.append(sub)
        sub._on_close = lambda: (
            self._subscriptions.remove(sub) if sub in self._subscriptions else None
        )
        return sub

    @staticmethod
    async def _run(handler: Handler, **data: Any) -> Any:
        result = handler(**data)
        if inspect.isawaitable(result):
            result = await result
        return result

    # — the three protocols —
    async def fire(self, event: str, **data: Any) -> None:
        """Observe: notify all callbacks and queue subscribers; callback errors
        never affect the main flow."""
        handlers = self._observers.get(event, ())
        results = await asyncio.gather(
            *[self._run(h, **data) for h in handlers], return_exceptions=True
        )
        for r in results:
            if isinstance(r, Exception):
                print(f"[bus] observer error ignored: {r!r}")
        # Backpressure delivery: a blocking subscription is awaited here, which
        # pushes pressure back to the event producer.
        await asyncio.gather(
            *[sub._deliver(event, data) for sub in self._subscriptions if sub._wants(event)]
        )

    async def check(self, gate: str, **data: Any) -> BlockingResult:
        """Adjudicate: ask in order; any veto or raised error blocks (fail-closed)."""
        for h in self._checkers.get(gate, ()):
            try:
                verdict = await self._run(h, **data)
            except Exception as exc:  # a broken checker never means "allow"
                return BlockingResult(False, f"checker error: {exc}")
            if verdict is False or (isinstance(verdict, BlockingResult) and not verdict.allowed):
                reason = (
                    verdict.reason if isinstance(verdict, BlockingResult) else f"vetoed by {gate}"
                )
                return BlockingResult(False, reason)
        return BlockingResult(True)

    async def collect(self, point: str, **data: Any) -> list[Any]:
        """Collect: aggregate the non-empty outputs of every provider."""
        out: list[Any] = []
        for h in self._providers.get(point, ()):
            value = await self._run(h, **data)
            if value is not None:
                out.append(value)
        return out
