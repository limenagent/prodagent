"""Multi-agent adapter for quiz_arena — maps WorkQueue + Blackboard events into
the unified :class:`MultiAgentEvent` envelope.

Two phases share one adapter and one ``stream()``:

- ``backstage_review`` (WorkQueue) — two reviewers race to claim and validate
  questions. All 5 questions pass validation and enter the live quiz.
- ``live_quiz`` (Blackboard) — host writes ``state`` with the next question;
  three contestants satisfy the ``buzz_in`` trigger, but only the lock winner
  computes — losers never have ``try_contribute`` called.
"""

from __future__ import annotations

from typing import Any

from prodagent.core.budget import HardBudget
from prodagent.playground.multiagent import (
    MultiAgentAdapter,
    MultiAgentEvent,
    ParticipantStatus,
    PhaseCompleted,
    PhaseStarted,
)
from prodagent.runtime.coordination.blackboard import (
    BlackboardCompletedEvent,
    BlackboardSpec,
    BoardWriteEvent,
    Trigger,
    blackboard_stream,
)
from prodagent.runtime.coordination.budget_ledger import BudgetLedger
from prodagent.runtime.coordination.termination import MaxRounds, TerminationPolicy
from prodagent.runtime.coordination.work_queue import (
    ItemClaimedEvent,
    ItemCompletedEvent,
    ItemDeadLetteredEvent,
    ItemRequeuedEvent,
    QueueDrainedEvent,
    WorkQueueSpec,
    work_queue_stream,
)

from quiz_arena.contestants import ContestantMember, build_contestant_agent
from quiz_arena.host import HostMember
from quiz_arena.questions import QUESTION_BANK, build_work_items
from quiz_arena.review import FlakyReviewer, QuickReviewer

_CONTESTANTS = [
    ("小明", "xiaoming", "地理和历史"),
    ("小红", "xiaohong", "文学"),
    ("小刚", "xiaogang", "科学"),
]


class QuizArenaAdapter:
    """Two-phase adapter: WorkQueue (backstage) → Blackboard (live).

    Stateful: tracks current phase, the set of validated question ids from
    phase 1, and a handle to each contestant member (for ``compute_count``
    introspection if a future caller wants it — not asserted here).
    """

    name = "quiz_arena"

    def __init__(self) -> None:
        self._phase: str | None = None
        self._validated: set[str] = set()
        self._by_id: dict[str, dict[str, Any]] = {q["id"]: q for q in QUESTION_BANK}
        self._contestant_members: list[ContestantMember] = []

    def initial_participants(self) -> list[ParticipantStatus]:
        participants: list[ParticipantStatus] = [
            ParticipantStatus(name="quick_reviewer", role="worker", state="idle", meta={"desc": "靠谱审核员"}),
            ParticipantStatus(name="flaky_reviewer", role="worker", state="idle", meta={"desc": "会失联的审核员"}),
            ParticipantStatus(name="host", role="host", state="idle", meta={"desc": "出题、判分"}),
        ]
        for name, _slug, specialty in _CONTESTANTS:
            participants.append(
                ParticipantStatus(name=name, role="expert", state="idle", meta={"specialty": specialty})
            )
        participants.append(
            ParticipantStatus(name="kickoff", role="trigger", state="idle", meta={"desc": "主持人常驻触发器"})
        )
        participants.append(
            ParticipantStatus(name="buzz_in", role="trigger", state="idle", meta={"desc": "抢答触发器·先抢锁再算"})
        )
        return participants

    def map_event(self, event: Any) -> MultiAgentEvent | list[MultiAgentEvent]:
        if isinstance(event, PhaseStarted):
            self._phase = event.phase
            return MultiAgentEvent(
                kind="phase_started",
                actor=None,
                phase=event.phase,
                summary={"verb": "phase_started", "object": event.phase},
                payload={"detail": event.detail} if event.detail else {},
                snapshot={},
            )
        if isinstance(event, PhaseCompleted):
            return MultiAgentEvent(
                kind="phase_completed",
                actor=None,
                phase=event.phase,
                summary={"verb": "phase_completed", "object": event.phase},
                payload={
                    "detail": event.detail if event.detail else "",
                    "counts": dict(event.counts) if event.counts else {},
                },
                snapshot={},
            )
        if isinstance(event, ItemClaimedEvent):
            return MultiAgentEvent(
                kind="claim",
                actor=event.worker,
                phase=self._phase,
                summary={"verb": "claimed", "object": event.item_id},
                payload={"item_id": event.item_id, "worker": event.worker},
                snapshot=event.queue_snapshot,
            )
        if isinstance(event, ItemCompletedEvent):
            self._validated.add(event.item_id)
            return MultiAgentEvent(
                kind="complete",
                actor=event.worker,
                phase=self._phase,
                summary={"verb": "completed", "object": event.item_id},
                payload={"item_id": event.item_id, "worker": event.worker},
                snapshot=event.queue_snapshot,
            )
        if isinstance(event, ItemRequeuedEvent):
            return MultiAgentEvent(
                kind="requeue",
                actor=event.worker,
                phase=self._phase,
                summary={"verb": "requeued", "object": event.item_id, "reason": event.reason},
                payload={"item_id": event.item_id, "worker": event.worker, "reason": event.reason},
                snapshot=event.queue_snapshot,
            )
        if isinstance(event, ItemDeadLetteredEvent):
            return MultiAgentEvent(
                kind="dead_letter",
                actor=event.worker,
                phase=self._phase,
                summary={
                    "verb": "dead-lettered",
                    "object": event.item_id,
                    "attempts": event.attempts,
                },
                payload={
                    "item_id": event.item_id,
                    "worker": event.worker,
                    "error": event.error,
                    "attempts": event.attempts,
                },
                snapshot=event.queue_snapshot,
            )
        if isinstance(event, QueueDrainedEvent):
            return MultiAgentEvent(
                kind="phase_completed",
                actor=None,
                phase=self._phase,
                summary={"verb": "queue_drained", "object": event.reason.reason},
                payload={
                    "detail": f"{event.reason.reason}: {event.reason.detail}",
                    "counts": {
                        "validated": len(self._validated),
                        "dead_lettered": event.queue_snapshot.get("dead_lettered", 0),
                    },
                },
                snapshot=event.queue_snapshot,
            )
        if isinstance(event, BoardWriteEvent):
            is_buzz_in = event.trigger_name == "buzz_in"
            return MultiAgentEvent(
                kind="write",
                actor=event.write.author,
                phase=self._phase,
                summary={"verb": "wrote", "object": event.write.key},
                payload={
                    "key": event.write.key,
                    "value": event.write.value,
                    "author": event.write.author,
                    "trigger_name": event.trigger_name,
                    "expected_version": event.write.expected_version,
                    "buzz_in_winner": is_buzz_in,
                },
                snapshot=event.board_snapshot,
            )
        if isinstance(event, BlackboardCompletedEvent):
            return MultiAgentEvent(
                kind="completed",
                actor=None,
                phase=self._phase,
                summary={"verb": "blackboard_completed", "object": event.reason.reason},
                payload={
                    "detail": f"{event.reason.reason}: {event.reason.detail}",
                    "final_state": (event.board_snapshot.get("slots") or {}).get("state", {}).get("value") or {},
                },
                snapshot=event.board_snapshot,
            )
        raise TypeError(f"QuizArenaAdapter.map_event: unknown event type {type(event).__name__}")

    async def stream(self):
        # Phase 1 — backstage review (WorkQueue).
        self._phase = "backstage_review"
        yield PhaseStarted("backstage_review", detail="审核员审题：两道审核员并行，快速审完所有题目")
        workers = {
            "quick_reviewer": QuickReviewer("quick_reviewer"),
            "flaky_reviewer": FlakyReviewer("flaky_reviewer"),
        }
        work_spec = WorkQueueSpec(
            workers=workers,
            items=build_work_items(),
            lease_seconds=0.02,
            termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=50)),
        )
        async for ev in work_queue_stream(work_spec):
            yield ev
        yield PhaseCompleted(
            "backstage_review",
            detail=f"{len(self._validated)}/{len(QUESTION_BANK)} 道题全部通过审核",
            counts={
                "通过": len(self._validated),
                "总计": len(QUESTION_BANK),
            },
        )

        # Phase 2 — live quiz (Blackboard).
        self._phase = "live_quiz"
        yield PhaseStarted("live_quiz", detail="主持人出题，三人抢答")
        ordered = [q for q in QUESTION_BANK if q["id"] in self._validated]
        members: dict[str, Any] = {"host": HostMember(ordered)}
        self._contestant_members = []
        for name, slug, specialty in _CONTESTANTS:
            agent = build_contestant_agent(name, specialty=specialty)
            cm = ContestantMember(agent, session_id=f"quiz-arena-live-{slug}")
            members[name] = cm
            self._contestant_members.append(cm)

        board_spec = BlackboardSpec(
            experts=members,  # type: ignore[arg-type]
            triggers={
                "kickoff": Trigger(name="kickoff", keys=[], experts=["host"], mode="event"),
                "buzz_in": Trigger(
                    name="buzz_in",
                    keys=["state"],
                    experts=[name for name, _, _ in _CONTESTANTS],
                    mode="buzz_in",
                ),
            },
            termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=100)),
            budget=BudgetLedger(max=HardBudget(max_turns=200, max_seconds=60, max_cost_usd=10.0)),
            terminal_check=lambda board: bool(
                ((board.read(["state"]) or {}).get("state") or {}).get("finished")
            ),
        )
        async for ev in blackboard_stream(board_spec):
            yield ev


def build_adapter() -> MultiAgentAdapter:
    """Factory called by :func:`discover_examples` — return a fresh adapter per run."""
    return QuizArenaAdapter()


__all__ = ["QuizArenaAdapter", "build_adapter"]
