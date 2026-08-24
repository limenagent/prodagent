"""Settler — everything that happens once, at the end of a run chain.

Takes a run in whatever state the chain left it and drives it to a durable,
observed terminal state: structured-output validation, the root's output
contract (an UPSTREAM crossing into the caller's world), checkpoint
persistence, and the RUN_COMPLETE gate / terminal events.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from prodagent.coordination.messaging.envelope import Crossing, CrossingKind, Direction
from prodagent.coordination.messaging.pipeline import admission_pipeline
from prodagent.core.exceptions import SECURITY_VETO_EXCEPTIONS
from prodagent.kernel.bus import Gate, HookEvent, fire as _fire
from prodagent.kernel.bus import save_and_fire_checkpoint
from prodagent.kernel.types import RunState

if TYPE_CHECKING:
    from pydantic import BaseModel

    from prodagent.coordination.messaging.contract import MessageContract
    from prodagent.runtime.runner import RunContext
    from prodagent.kernel.bus import HookRegistry
    from prodagent.kernel.state import AgentRun

logger = logging.getLogger(__name__)

__all__ = ["Settler"]


class Settler:
    """Terminal discipline for one run chain, extracted from RunLoop."""

    def __init__(
        self,
        *,
        agent_name: str,
        root_run_id: str,
        output_schema: type[BaseModel] | None = None,
        output_contract: MessageContract | None = None,
    ) -> None:
        self._agent_name = agent_name
        self._root_run_id = root_run_id
        self._output_schema = output_schema
        self._output_contract = output_contract

    async def settle(self, run: AgentRun, ctx: RunContext, hooks: HookRegistry | None) -> None:
        if run.state is RunState.SUSPENDED:
            await self._save_checkpoint(run, ctx, hooks)
            return

        if run.state is not RunState.FAILED:
            run.state = RunState.COMPLETED
            await self._validate_structured_output(run)
            await self._admit_output_contract(run)

        if run.state is RunState.FAILED:
            if run.checkpoint_version > 0:
                await self._save_checkpoint(run, ctx, hooks)
        else:
            await self._save_checkpoint(run, ctx, hooks)

        if hooks is None:
            return

        if run.state is RunState.FAILED:
            await _fire(
                hooks,
                HookEvent.RUN_FAILED,
                run_id=run.run_id,
                turns=run.turn_count,
                total_tokens=run.total_tokens,
                cost_usd=run.cost_usd,
                elapsed_s=run.elapsed_seconds(),
                state=run.state.value,
                error=run.last_error or "",
            )
            return

        try:
            await hooks.check_blocking(
                Gate.RUN_COMPLETE,
                run_id=run.run_id,
                final_output=run.final_output or "",
                run=run,
                turns=run.turn_count,
                cost_usd=run.cost_usd,
                state=run.state.value,
            )
        except SECURITY_VETO_EXCEPTIONS:
            run.state = RunState.FAILED
            await self._save_checkpoint(run, ctx, hooks)
            raise

        await _fire(
            hooks,
            HookEvent.RUN_COMPLETE,
            run_id=run.run_id,
            turns=run.turn_count,
            total_tokens=run.total_tokens,
            cost_usd=run.cost_usd,
            elapsed_s=run.elapsed_seconds(),
            state=run.state.value,
        )

    async def _validate_structured_output(self, run: AgentRun) -> None:
        if self._output_schema is None or not run.final_output:
            return
        try:
            from prodagent.llm.structured_output import parse_json_as

            run.structured_output = parse_json_as(run.final_output, self._output_schema)
        except Exception as exc:
            run.state = RunState.FAILED
            run.last_error = f"structured output validation failed: {exc}"
            logger.warning("Settler[%s] structured output parse failed: %s", run.run_id, exc)

    async def _admit_output_contract(self, run: AgentRun) -> None:
        """The chain root's final output is an UPSTREAM crossing into the
        caller's world — a declared contract is admitted here, mirroring the
        structured-output gate."""
        if self._output_contract is None or run.state is not RunState.COMPLETED:
            return
        delivery = await admission_pipeline(contract=self._output_contract).process(
            Crossing.mint(
                direction=Direction.UPSTREAM,
                kind=CrossingKind.RESULT,
                from_agent=self._agent_name,
                to="caller",
                payload={
                    "agent": self._agent_name,
                    "output": run.final_output or "",
                    "state": run.state.value,
                },
                trace_id=self._root_run_id,
                message_id=f"{self._root_run_id}:settle",
            )
        )
        if delivery.status == "rejected":
            run.state = RunState.FAILED
            run.last_error = f"contract violation: {delivery.reason}"
            logger.warning(
                "Settler[%s] root output rejected by contract: %s",
                run.run_id,
                delivery.reason,
            )

    async def _save_checkpoint(
        self,
        run: AgentRun,
        ctx: RunContext,
        hooks: HookRegistry | None = None,
    ) -> None:
        if ctx.checkpoint is not None:
            await save_and_fire_checkpoint(ctx.checkpoint, run, hooks)
