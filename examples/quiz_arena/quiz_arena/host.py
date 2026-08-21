"""主持人 —— 纯 Python 的 ``BlackboardMember``，keys=[] 的常驻 kickoff 触发器。

只在真的有状态变化时才写 ``state``（评完一题、出下一题、或者宣布结束）——
如果每轮都无条件写一次，哪怕内容没变也会让 ``state`` 版本号跳动，进而把
keys=["state"] 的抢答触发器重新炸出来，选手会对着同一道还没揭晓结果的题
反复重新抢答。"nothing changed → return None" 是这里的关键约束。
"""

from __future__ import annotations

from collections import deque
from typing import Any

from prodagent import Board, BoardWrite, Trigger


class HostMember:
    name = "host"

    def __init__(self, questions: list[dict[str, Any]]) -> None:
        self._pending: deque[dict[str, Any]] = deque(questions)
        self._current: dict[str, Any] | None = None
        self._graded_ids: set[str] = set()
        self._scores: dict[str, int] = {}
        self._log: list[str] = []
        self._finished = False

    async def try_contribute(self, board: Board, *, trigger: Trigger) -> BoardWrite | None:
        if self._finished:
            return None

        changed = False
        answer = (board.read(["answer"]) or {}).get("answer")
        if (
            self._current is not None
            and answer is not None
            and answer["question_id"] == self._current["id"]
            and answer["question_id"] not in self._graded_ids
        ):
            self._grade(answer)
            self._graded_ids.add(answer["question_id"])
            self._current = None
            changed = True

        if self._current is None:
            if self._pending:
                self._current = self._pending.popleft()
            else:
                self._finished = True
            changed = True

        if not changed:
            return None

        return BoardWrite(
            key="state",
            value={
                "current_question": self._current,
                "scores": dict(self._scores),
                "log": list(self._log),
                "finished": self._finished,
            },
            author=self.name,
        )

    def _grade(self, answer: dict[str, Any]) -> None:
        assert self._current is not None
        correct = self._current["answer"] in answer["text"] or answer["text"] in self._current["answer"]
        contestant = answer["contestant"]
        if correct:
            self._scores[contestant] = self._scores.get(contestant, 0) + 1
        verdict = "✓ 正确" if correct else "✗ 错误"
        self._log.append(
            f"{contestant} 答“{answer['text']}” —— {verdict}（标准答案：{self._current['answer']}）"
        )


__all__ = ["HostMember"]
