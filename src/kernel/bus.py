"""bus —— 内核对外的唯一接缝。

两套用法，服务两类消费者：

1. 横切插件用“回调三协议”：
   - fire：旁观。可观测、记账、审计挂这里。旁观者出错不影响主流程。
   - check：裁决。审批、权限、预算闸门挂这里，任一否决即阻断；
     安全默认 fail-closed：裁决器自己抛异常也按否决处理，绝不放行。
   - collect：收集。装配 prompt 时收集各插件想额外注入的片段。

2. 流式消费用“订阅队列 + 背压”：
   subscribe() 返回一个有界队列，token 这类高频事件往里投；队列满时两种策略——
   "block" 会让生产端在投递处等待，从而把压力一路反压回模型流；
   "drop" 不阻塞、满了就丢并计数，适合允许丢帧的进度展示。
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
    """一个有界事件订阅：await get() 或 async for 取事件。"""

    def __init__(self, kinds: tuple[str, ...], maxsize: int, on_full: str):
        self.kinds = kinds  # 空元组表示订阅全部
        self.on_full = on_full
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0  # drop 策略下累计丢弃的帧数
        self._closed = False
        self._on_close = None  # 由 Bus 注入：关闭时把自己从总线摘除

    def _wants(self, event: str) -> bool:
        return not self.kinds or event in self.kinds

    async def _deliver(self, event: str, data: dict) -> None:
        if not self._wants(event) or self._closed:
            return
        item = {"event": event, **data}
        if self.on_full == "block":
            await self.queue.put(item)  # 满了就在这里等，反压生产端
            return
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self.dropped += 1  # drop：不阻塞，记一笔账

    async def get(self) -> dict:
        return await self.queue.get()

    def close(self) -> None:
        self._closed = True
        if self._on_close is not None:  # 顺手从总线注销，避免订阅只增不减
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

    # —— 回调注册 ——
    def on(self, event: str, handler: Handler) -> None:
        self._observers.setdefault(event, []).append(handler)

    def checker(self, gate: str, handler: Handler) -> None:
        self._checkers.setdefault(gate, []).append(handler)

    def provider(self, point: str, handler: Handler) -> None:
        self._providers.setdefault(point, []).append(handler)

    # —— 流式订阅（背压）——
    def subscribe(self, *kinds: str, maxsize: int = 0, on_full: str = "block") -> Subscription:
        if on_full not in ("block", "drop"):
            raise ValueError("on_full 只能是 'block' 或 'drop'")
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

    # —— 三协议 ——
    async def fire(self, event: str, **data: Any) -> None:
        """旁观：通知所有回调与队列订阅；回调出错不影响主流程。"""
        handlers = self._observers.get(event, ())
        results = await asyncio.gather(
            *[self._run(h, **data) for h in handlers], return_exceptions=True
        )
        for r in results:
            if isinstance(r, Exception):
                print(f"[bus] observer error ignored: {r!r}")
        # 背压投递：block 订阅会在此处被 await，从而把压力传回事件生产端。
        await asyncio.gather(
            *[sub._deliver(event, data) for sub in self._subscriptions if sub._wants(event)]
        )

    async def check(self, gate: str, **data: Any) -> BlockingResult:
        """裁决：顺序询问，任一否决或抛错都阻断（fail-closed）。"""
        for h in self._checkers.get(gate, ()):
            try:
                verdict = await self._run(h, **data)
            except Exception as exc:  # 裁决器自身故障 → 不放行
                return BlockingResult(False, f"checker error: {exc}")
            if verdict is False or (isinstance(verdict, BlockingResult) and not verdict.allowed):
                reason = (
                    verdict.reason if isinstance(verdict, BlockingResult) else f"被 {gate} 否决"
                )
                return BlockingResult(False, reason)
        return BlockingResult(True)

    async def collect(self, point: str, **data: Any) -> list[Any]:
        """收集：汇总各 provider 的非空产物。"""
        out: list[Any] = []
        for h in self._providers.get(point, ()):
            value = await self._run(h, **data)
            if value is not None:
                out.append(value)
        return out
