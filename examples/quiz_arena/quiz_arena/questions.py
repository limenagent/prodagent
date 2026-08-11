"""题库 —— 5 道候选题，涵盖地理、文学、科学、历史。
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
        "id": "q4",
        "category": "科学",
        "text": "光在真空中的传播速度约为每秒多少公里？",
        "answer": "约30万公里",
    },
    {
        "id": "q5",
        "category": "历史",
        "text": "第一次世界大战爆发于哪一年？",
        "answer": "1914年",
    },
]


def build_work_items() -> list[WorkItem]:
    return [WorkItem(item_id=q["id"], payload=q) for q in QUESTION_BANK]


__all__ = ["QUESTION_BANK", "build_work_items"]
