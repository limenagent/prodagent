"""后台审核员 —— WorkQueue 的 ``Worker`` 实现，纯 Python 业务规则，不接 LLM。

题目审核是规则判断（有没有题干、有没有答案），不需要 Agent 参与——这里刻意
用最朴素的 ``Worker`` 实现示范"不是所有专家都得是 LLM"，跟 :mod:`contestants`
里真正用 Agent 的选手形成对照。
"""

from __future__ import annotations

import asyncio

from prodagent.runtime.coordination.work_queue import SharedQueue, WorkResult


def _is_valid(payload: dict) -> bool:
    return bool(payload.get("text")) and bool(payload.get("answer"))


class QuickReviewer:
    """永远靠谱的审核员——认领就立刻审完。"""

    def __init__(self, name: str) -> None:
        self.name = name

    async def try_claim_and_run(self, queue: SharedQueue, *, name: str) -> WorkResult | None:
        item = await queue.claim_next(name)
        if item is None:
            return None
        if not _is_valid(item.payload):
            return WorkResult(
                item_id=item.item_id, outcome="failure", error="题干或答案缺失，判定无效题目"
            )
        return WorkResult(item_id=item.item_id, outcome="success")


class FlakyReviewer:
    """对 ``hang_on`` 那道题只会失联一次——认领后卡住，从不回报，模拟审核员
    进程中途崩溃。租约到期后队列会把它当成一次失败处理，重新排队；重新被
    认领时（哪怕还是这个审核员）就会走正常路径审完。"""

    def __init__(self, name: str, *, hang_on: str) -> None:
        self.name = name
        self._hang_on = hang_on
        self._has_hung = False

    async def try_claim_and_run(self, queue: SharedQueue, *, name: str) -> WorkResult | None:
        item = await queue.claim_next(name)
        if item is None:
            return None
        if item.item_id == self._hang_on and not self._has_hung:
            self._has_hung = True
            # 卡住一小段真实时间——比 lease_seconds 长，保证下一轮扫描时
            # 这次认领的租约确实已经过期，而不是靠人为造假的负数租约。
            await asyncio.sleep(0.05)
            return None  # 从不回报 —— 队列只能靠租约超时发现它"失联"了。
        if not _is_valid(item.payload):
            return WorkResult(
                item_id=item.item_id, outcome="failure", error="题干或答案缺失，判定无效题目"
            )
        return WorkResult(item_id=item.item_id, outcome="success")


__all__ = ["QuickReviewer", "FlakyReviewer"]
