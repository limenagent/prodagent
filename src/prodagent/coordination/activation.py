"""activate_subagent — the one activation core every delegation path shares.

Whether a delegation arrives as a tool call the model made mid-Turn
(``spawn_agent``) or as a node written into the graph (SubAgentBody), the
execution underneath is identical: resolve the target, activate it through
the RunnerPort with parentage and the chained ledger, clamp it to its own
budget's clock, and fold the child run's terminal state into a
:class:`ChildResult` the caller can bill on and resume from. That core
lives here once — column 26's "one execution core, many entry points".

What does NOT live here: the governance shell around the tool form
(dispatch dedupe, admission, dead letters) — those belong to the spawn
tool, and a graph node skips them by design (the graph IS the dispatch).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from prodagent.base.errors import SECURITY_VETO_EXCEPTIONS
from prodagent.coordination.spawn import ChildResult, short_result
from prodagent.kernel.state import collect_final_run
from prodagent.kernel.types import RunState, ToolResult
from prodagent.ports.execution import AgentActivation

if TYPE_CHECKING:
    from prodagent.kernel.budget import BudgetLedger
    from prodagent.ports.execution import RunnerPort
    from prodagent.runtime.agent import Agent

logger = logging.getLogger(__name__)

STATE_FAILED = "failed"
STATE_TIMEOUT = "timeout"

DEFAULT_TIMEOUT_S = 900.0


async def activate_subagent(
    runner: RunnerPort,
    spec: Agent,
    task: str,
    *,
    parent_run_id: str | None,
    depth: int,
    budget_ledger: BudgetLedger | None = None,
    child_run_id: str | None = None,
    default_timeout_s: float = DEFAULT_TIMEOUT_S,
) -> ChildResult:
    """Activate one child agent through the port and fold its terminal run.

    Wall-clock clamp is the child's own budget's seconds axis, not a guess.
    A timeout is a *result*, not an exception: the caller reads "the child
    ran past its clock" and decides (typically: don't blind-retry)."""
    # NB: `child_run_id` is also this function's parameter — alias the
    # helper instead of shadowing it, or the function object rides home as
    # the run id (found the hard way: one TypeError in safe_filename).
    from prodagent.kernel.state import child_run_id as mint_child_id

    resolved_child_id = child_run_id or (
        mint_child_id(parent_run_id, spec.name) if parent_run_id else None
    )
    timeout = (
        spec.budget_config.max_seconds if spec.budget_config is not None else default_timeout_s
    )
    try:
        return await asyncio.wait_for(
            _activate_and_fold(
                runner,
                spec,
                task,
                parent_run_id=parent_run_id,
                depth=depth,
                budget_ledger=budget_ledger,
                child_run_id=resolved_child_id,
            ),
            timeout=timeout,
        )
    except TimeoutError:
        return short_result(spec.name, STATE_TIMEOUT, f"Sub-agent timed out after {timeout:.0f}s")
    except SECURITY_VETO_EXCEPTIONS:
        raise
    except Exception as exc:
        logger.error("Sub-agent %r failed: %s", spec.name, exc, exc_info=True)
        return short_result(spec.name, STATE_FAILED, str(exc), failed_reason="raised")


async def _activate_and_fold(
    runner: RunnerPort,
    spec: Agent,
    task: str,
    *,
    parent_run_id: str | None,
    depth: int,
    budget_ledger: BudgetLedger | None,
    child_run_id: str | None,
) -> ChildResult:
    """One activation, reduced to its terminal run — where the child executes
    (this process or another machine) is the port implementation's business."""
    try:
        run = await collect_final_run(
            runner.activate(
                AgentActivation(
                    agent=spec,
                    task=task,
                    run_id=child_run_id,
                    parent_run_id=parent_run_id,
                    depth=depth,
                    budget_ledger=budget_ledger,
                )
            ),
            fallback_run_id=child_run_id or spec.name,
            fallback_task=task,
        )
    except SECURITY_VETO_EXCEPTIONS:
        raise
    except Exception as exc:
        logger.error("Sub-agent %r failed: %s", spec.name, exc, exc_info=True)
        return short_result(spec.name, STATE_FAILED, str(exc), failed_reason="raised")

    output = run.final_output or run.last_error or ""
    return ChildResult(
        agent=spec.name,
        state=run.state.value,
        output=output,
        turns=run.turn_count,
        cost_usd=round(run.cost_usd, 4),
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        tool_history=list(run.tool_history),
        approval_request_id=run.pending_approval_id or "",
        failed_reason="failed" if run.state is RunState.FAILED else None,
    )


def child_result_to_outcome(result: ChildResult) -> ToolResult:
    """A ChildResult as a node outcome envelope — how a SubAgentBody folds
    the child's terminal state into the parent graph: completion carries the
    report, suspension parks the parent node on the same approval, failure
    becomes red feedback (replanning IS the recovery)."""
    from prodagent.base.errors import ErrorReason
    from prodagent.kernel.types import ToolError, ToolOutcome

    if result.state == "completed":
        return ToolResult(
            ToolOutcome.OK,
            value={
                "agent": result.agent,
                "state": result.state,
                "output": result.output,
                "turns": result.turns,
                "cost_usd": result.cost_usd,
            },
            tool="subagent",
        )
    if result.state == "suspended":
        return ToolResult.suspended(
            reason=f"child {result.agent!r} awaiting approval",
            tool="subagent",
            approval_request_id=result.approval_request_id,
        )
    return ToolResult.from_error(
        ToolError.from_reason(
            ErrorReason.UNKNOWN,
            code="subagent_failed",
            message=result.output or f"child {result.agent!r} ended {result.state}",
        ),
        tool="subagent",
    )
