"""题库 —— 5 道候选题，其中 2 道天生有问题，用来在 WorkQueue 阶段被淘汰。

``q4``/``q5`` 缺题干或缺答案，任何审核员都会判定失败——重试到上限后进
死信，永远不会进入正式抢答环节。``q2`` 被 :class:`~quiz_arena.review.FlakyReviewer`
用来演示"审核员失联"：第一次认领后直接挂掉不回报，租约到期被收回重新入队，
下一次认领（可能是同一个审核员，也可能是另一个）才会真正审完。
"""

from __future__ import annotations

from typing import Any

from prodagent.runtime.coordination.work_queue import WorkItem

QUESTION_BANK: list[dict[str, Any]] = [
    {
        "id": "q1",
        "category": "地理",
        "text": "世界上流量最大的河流是哪条河？",
        "answer": "亚马逊河",
    },
    {
        "id": "q2",
        "category": "文学",
        "text": "《红楼梦》的作者是谁？",
        "answer": "曹雪芹",
    },
    {
        "id": "q3",
        "category": "科学",
        "text": "人体中面积最大的器官是什么？",
        "answer": "皮肤",
    },
    {
        # 缺题干——任何审核员都会拒绝，用来演示死信归档。
        "id": "q4",
        "category": "科学",
        "text": "",
        "answer": "光速",
    },
    {
        # 缺答案——同上。
        "id": "q5",
        "category": "历史",
        "text": "第一次世界大战爆发于哪一年？",
        "answer": "",
    },
]


def build_work_items() -> list[WorkItem]:
    return [WorkItem(item_id=q["id"], payload=q) for q in QUESTION_BANK]


__all__ = ["QUESTION_BANK", "build_work_items"]
