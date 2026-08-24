"""Event-sourcing + checkpoint persistence for PLAN_FIRST execution."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from prodagent.core.event_log import (
    Event,
    PlanEventType,
    hybrid_restore,
)
from prodagent.hooks import save_and_fire_checkpoint

if TYPE_CHECKING:
    from prodagent.kernel.state import AgentRun
    from prodagent.kernel.bus import HookRegistry
    from prodagent.plan.dag import Plan
    from prodagent.ports import CheckpointStore, EventLog

logger = logging.getLogger(__name__)


def apply_event(state: dict[str, Any], event: Event) -> None:
    """Reducer for ``hybrid_restore``."""
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
        event_log: EventLog | None = None,
        checkpoint_store: CheckpointStore | None = None,
        *,
        framework_config: Any = None,
        hooks: HookRegistry | None = None,
    ) -> None:
        if framework_config is None and (event_log is None or checkpoint_store is None):
            raise ValueError(
                "PlanEventLog requires either explicit stores or a framework_config to resolve them"
            )
        from prodagent.backends.factory import resolve_checkpoint, resolve_event_log

        self._events = event_log or resolve_event_log(framework_config)
        self._checkpoints = checkpoint_store or resolve_checkpoint(framework_config)
        self._framework_config = framework_config
        self._hooks = hooks
        self._lock = asyncio.Lock()

    async def has_events(self, run_id: str) -> bool:
        return bool(await self._events.get_events(run_id))

    async def has_resumable_state(self, run_id: str) -> bool:
        """Either event log has events, or checkpoint carries ``plan_state`` (forked-run path: empty event log but self-contained plan_state). Without this gate ``_prepare_run`` would discard forked plan state."""
        if await self.has_events(run_id):
            return True
        stored = await self._checkpoints.load(run_id)
        return stored is not None and stored.plan_state is not None

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
        state, ckpt_version, last_seq = await hybrid_restore(
            run.run_id, self._events, self._checkpoints, apply_event
        )
        run.checkpoint_version = max(run.checkpoint_version, ckpt_version)
        run.plan_last_seq = max(run.plan_last_seq, last_seq)
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
                run.plan_state = plan.to_state()
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
        async with self._lock:
            seq = await self._events.append(
                Event.make(event_type, plan_id=run.run_id, version=version, **data),
                expected_seq=run.plan_last_seq,
            )
            run.plan_last_seq = seq
            if checkpoint_plan is not None:
                run.plan_state = checkpoint_plan.to_state()
                await save_and_fire_checkpoint(self._checkpoints, run, self._hooks)
            return seq
