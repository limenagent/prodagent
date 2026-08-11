"""后台审核员 —— WorkQueue 的 ``Worker`` 实现。

每个审核员认领题目后检查题干和答案是否完整，不完整的判定失败。
"""

from __future__ import annotations

from prodagent.runtime.coordination.work_queue import SharedQueue, WorkResult


def _is_valid(payload: dict) -> bool:
    return bool(payload.get("text")) and bool(payload.get("answer"))


class QuickReviewer:
    """靠谱审核员——认领就立刻审完。"""

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
    """另一个审核员——和 QuickReviewer 一起抢题，提高审核速度。"""

    def __init__(self, name: str, *, hang_on: str = "") -> None:
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


__all__ = ["QuickReviewer", "FlakyReviewer"]
