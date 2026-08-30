"""PlanEventLog — event sourcing + checkpoint persistence for PLAN_FIRST.

Recovery here is snapshot-based with tail replay: the checkpoint stores a
plan snapshot *and the event seq it was taken at* (``last_seq``); restore
folds the snapshot first, then replays only the events after it. Snapshots
truncate replay length; the event stream guarantees no increment is lost —
the same trade a WAL-plus-checkpoint database makes. Every mutation of the
plan (created / started / completed / failed / suspended / replanned) is
one appended event, so any state is reproducible from the log alone.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from prodagent.base.event_log import (
    Event,
    PlanEventType,
    append_expected,
    hybrid_restore,
)
from prodagent.hooks import save_and_fire_checkpoint

if TYPE_CHECKING:
    from prodagent.kernel.bus import HookRegistry
    from prodagent.kernel.state import AgentRun
    from prodagent.plan.dag import Plan
    from prodagent.ports import CheckpointStore, EventLog

logger = logging.getLogger(__name__)


# ── Plan cursor — this domain's boxed section on AgentRun ─────────────────────


def _plan_tail(run: AgentRun) -> dict[str, Any]:
    """The plan cursor section (``run.cursors["plan"]``); ``{}`` when absent."""
    tail = run.cursor("plan")
    return tail if isinstance(tail, dict) else {}


def _plan_state(run: AgentRun) -> dict[str, Any] | None:
    return _plan_tail(run).get("state")


def _plan_last_seq(run: AgentRun) -> int:
    return int(_plan_tail(run).get("last_seq") or 0)


def _set_plan(run: AgentRun, *, state: Any, last_seq: int) -> None:
    run.set_cursor("plan", {"state": state, "last_seq": last_seq})


def apply_event(state: dict[str, Any], event: Event) -> None:
    """Reducer for ``hybrid_restore`` — a pure ``(state, event) → state``
    fold. Same event sequence, same state, every time: that determinism is
    what makes crash recovery trustworthy and the reducer testable alone."""

    steps = state.setdefault("steps", {})
    match event.event_type:
        case PlanEventType.PLAN_CREATED:
            state["version"] = event.version
            state["steps"] = {s["step_id"]: s for s in event.data.get("steps", [])}
        case PlanEventType.STEP_STARTED:
            if (s := steps.get(event.data.get("step_id", ""))) is not None:
                s["status"] = "running"
        case PlanEventType.STEP_COMPLETED:
            if (s := steps.get(event.data.get("step_id", ""))) is not None:
                s["status"] = "completed"
                s["output_ref"] = event.data.get("output_ref")
        case PlanEventType.STEP_FAILED:
            if (s := steps.get(event.data.get("step_id", ""))) is not None:
                s["status"] = "failed"
                s["error"] = event.data.get("error")
        case PlanEventType.STEP_SUSPENDED:
            if (s := steps.get(event.data.get("step_id", ""))) is not None:
                s["status"] = "suspended"
        case PlanEventType.PLAN_REPLANNED:
            state["version"] = event.version
            for ns in event.data.get("new_steps", []):
                replaces = ns.get("replaces_step_id")
                if replaces and replaces in steps:
                    steps[replaces]["status"] = "obsolete"
                steps[ns["step_id"]] = ns


class PlanEventLog:
    """Append-only event log + checkpoint store, serialised under one lock."""

    _events: EventLog
    _checkpoints: CheckpointStore

    def __init__(
        self,
        event_log: EventLog,
        checkpoint_store: CheckpointStore,
        *,
        hooks: HookRegistry | None = None,
    ) -> None:
        self._events = event_log
        self._checkpoints = checkpoint_store
        self._hooks = hooks
        self._lock = asyncio.Lock()

    @property
    def event_log(self) -> EventLog:
        """The WAL this domain log rides on — the driver exposes it to the
        run scope so observers (span recording) reach the fact pipeline."""
        return self._events

    async def has_events(self, run_id: str) -> bool:
        return bool(await self._events.get_events(run_id))

    async def has_resumable_state(self, run_id: str) -> bool:
        """Either event log has events, or checkpoint carries ``plan_state`` (forked-run path: empty event log but self-contained plan_state). Without this gate ``_prepare_run`` would discard forked plan state."""
        if await self.has_events(run_id):
            return True
        stored = await self._checkpoints.load(run_id)
        return stored is not None and _plan_state(stored) is not None

    async def rebaseline_checkpoint(self, run: AgentRun) -> None:
        """Ensures first checkpoint save uses the correct expected_version (avoids collision with stale snapshot from a prior failed run)."""
        stored = await self._checkpoints.load(run.run_id)
        if stored is not None:
            run.checkpoint_version = max(run.checkpoint_version, stored.checkpoint_version)

    async def restore_plan(self, run: AgentRun) -> dict[str, Any]:
        """Restores the checkpointed run so a resumed plan continues the SAME
        logical execution — not a fresh one that happens to share the run_id.

        Beyond ``pending_approval_id`` (the HITL gate), the trajectory and the
        accounting carry forward: zeroing ``tool_history`` / ``metrics`` on
        resume masks cost and turn regressions in evals, and resetting
        ``idempotency_seq`` would re-derive keys already consumed before the
        suspend (INV-IDEM-03: anchors roll back WITH the checkpoint).
        """
        state: dict[str, Any]
        state, ckpt_version, last_seq = await hybrid_restore(
            run.run_id,
            self._events,
            self._checkpoints,
            apply_event,
            extract_base=lambda r: (
                (_plan_state(r), r.checkpoint_version, _plan_last_seq(r))
                if _plan_state(r) is not None
                else None
            ),
            empty_state=lambda: {"steps": {}, "version": 0},
        )
        run.checkpoint_version = max(run.checkpoint_version, ckpt_version)
        _set_plan(run, state=_plan_state(run), last_seq=max(_plan_last_seq(run), last_seq))
        stored = await self._checkpoints.load(run.run_id)
        if stored is None:
            return state
        run.metrics = stored.metrics
        run.start_time = stored.start_time
        run.tool_history = list(stored.tool_history)
        run.tool_failures = stored.tool_failures
        run.retry_counter = dict(stored.retry_counter)
        run.fingerprints = list(stored.fingerprints)
        run.idempotency_seq = stored.idempotency_seq
        if stored.pending_approval_id:
            run.pending_approval_id = stored.pending_approval_id
        return state

    async def record_plan_created(self, plan: Plan, run: AgentRun) -> int:
        return await self._record(
            run,
            PlanEventType.PLAN_CREATED,
            plan.version,
            steps=[s.to_dict() for s in plan.steps],
        )

    async def record_step_started(self, plan: Plan, run: AgentRun, step_id: str) -> int:
        return await self._record(
            run,
            PlanEventType.STEP_STARTED,
            plan.version,
            step_id=step_id,
        )

    async def record_step_completed(
        self,
        plan: Plan,
        run: AgentRun,
        step_id: str,
        result: Any,
    ) -> int:
        return await self._record(
            run,
            PlanEventType.STEP_COMPLETED,
            plan.version,
            step_id=step_id,
            output_ref=result,
            checkpoint_plan=plan,
        )

    async def record_step_failed(
        self,
        plan: Plan,
        run: AgentRun,
        step_id: str,
        error: str,
    ) -> int:
        return await self._record(
            run,
            PlanEventType.STEP_FAILED,
            plan.version,
            step_id=step_id,
            error=error,
        )

    async def record_step_suspended(
        self,
        plan: Plan,
        run: AgentRun,
        step_id: str,
    ) -> int:
        return await self._record(
            run,
            PlanEventType.STEP_SUSPENDED,
            plan.version,
            step_id=step_id,
            checkpoint_plan=plan,
        )

    async def record_replanned(
        self,
        plan: Plan,
        run: AgentRun,
        new_steps: list[Any],
    ) -> int:
        return await self._record(
            run,
            PlanEventType.PLAN_REPLANNED,
            plan.version,
            new_steps=[s.to_dict() for s in new_steps],
            checkpoint_plan=plan,
        )

    async def save_snapshot(self, run: AgentRun, *, plan: Plan | None = None) -> None:
        """No step event to record, but ``pending_approval_id`` must survive a resume (HITL-suspended plan)."""
        async with self._lock:
            if plan is not None:
                _set_plan(run, state=plan.to_state(), last_seq=_plan_last_seq(run))
            await save_and_fire_checkpoint(self._checkpoints, run, self._hooks)

    async def _record(
        self,
        run: AgentRun,
        event_type: PlanEventType,
        version: int,
        *,
        checkpoint_plan: Plan | None = None,
        **data: Any,
    ) -> int:
        """The one serialized mutation path: append with the optimistic
        tail-check, advance the cursor to the returned seq, and — for the
        events resume parks on (completed / suspended / replanned) — write
        a fresh snapshot under it. Locking here keeps append and checkpoint
        from interleaving across the concurrently-running steps of a wave."""
        async with self._lock:
            seq = await append_expected(
                self._events,
                Event.make(event_type, stream_id=run.run_id, version=version, **data),
                tail_seq=_plan_last_seq(run),
            )
            _set_plan(run, state=_plan_state(run), last_seq=seq)
            if checkpoint_plan is not None:
                _set_plan(run, state=checkpoint_plan.to_state(), last_seq=_plan_last_seq(run))
                await save_and_fire_checkpoint(self._checkpoints, run, self._hooks)
            return seq
