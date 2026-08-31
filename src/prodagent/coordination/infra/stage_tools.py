"""Stage tools — convene a stage topology the way you spawn a child.

The model can call stage tools
fan out a queue, exactly like it calls ``spawn_agent``. The specs are
declared once on ``AgentConfig``; the
tool runs the same stream entry points the application layer uses and
returns a summary the calling agent can read.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from prodagent.coordination.blackboard import (
    BlackboardCompletedEvent,
    blackboard_stream,
)
from prodagent.kernel.types import SideEffectLevel, ToolMeta
from prodagent.tooling.base import FunctionTool
from prodagent.tooling.merge import attach_tools

if TYPE_CHECKING:
    from collections.abc import Callable

    from prodagent.coordination.blackboard import BlackboardSpec
    from prodagent.kernel.budget import BudgetLedger
    from prodagent.ports import Tool

logger = logging.getLogger(__name__)

__all__ = ["build_stage_tools", "assemble_stage_tools"]


def _by_name(specs: list[Any]) -> dict[str, Any]:
    return {s.name: s for s in specs if getattr(s, "name", "")}


def _blackboard_tool(
    specs: list[BlackboardSpec], budget: Callable[[], BudgetLedger | None]
) -> FunctionTool:
    roster = "\n".join(f"  - {s.name}: experts {sorted(s.experts)}" for s in specs if s.name)

    async def _fn(name: str, seeds: dict[str, str] | None = None) -> dict[str, Any]:
        spec = _by_name(specs).get(name)
        if spec is None:
            return {
                "error": True,
                "reason": f"unknown blackboard {name!r}",
                "known": list(_by_name(specs)),
            }
        spec = replace(spec, seed=seeds or spec.seed, budget=spec.budget or budget())
        completed: BlackboardCompletedEvent | None = None
        async for event in blackboard_stream(spec):
            if isinstance(event, BlackboardCompletedEvent):
                completed = event
        if completed is None:  # pragma: no cover — the driver always finalizes
            return {"error": True, "reason": "blackboard stream ended without a terminal event"}
        return {
            "blackboard": name,
            "reason": completed.reason.reason,
            "slots": completed.board_snapshot["slots"],
        }

    schema = {
        "name": "run_blackboard",
        "description": (
            "Run a declared blackboard — experts contribute to shared slots as"
            " their triggers fire; you get the final slot values back. Seed"
            " keys to kick off the first round.\n"
            f"Available blackboards:\n{roster}"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "enum": list(_by_name(specs))},
                "seeds": {
                    "type": "object",
                    "description": "Initial slot values to write before the first round.",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["name"],
        },
    }
    meta = ToolMeta(
        name="run_blackboard",
        side_effect_level=SideEffectLevel.LOW,
        is_readonly=True,
        domain="orchestration",
        timeout_seconds=600.0,
    )
    return FunctionTool(name="run_blackboard", fn=_fn, meta=meta, schema=schema)


def build_stage_tools(
    *,
    blackboards: list[BlackboardSpec] | None = None,
    budget_ledger: BudgetLedger | None = None,
) -> list[FunctionTool]:
    """Build the stage-convening tools for the named specs. Unnamed specs are
    skipped — a name is what makes a spec callable by the model."""
    tools: list[FunctionTool] = []
    named_boards = [s for s in (blackboards or []) if s.name]
    if named_boards:
        tools.append(_blackboard_tool(named_boards, lambda: budget_ledger))
    return tools


def assemble_stage_tools(
    ctx: Any,
    active_tools: list[Tool],
    tool_schemas: list[dict[str, Any]],
    spawn_acc: Any = None,
) -> Any:
    """Mount ``run_blackboard`` for the specs declared on
    ``agent.config``. Returns ``spawn_acc`` unchanged — stage tools keep no
    accumulator, but the pass-through keeps the assembler call shape
    symmetric. ``ctx`` is the hop's RunContext — runtime vocabulary, read
    structurally (same contract as the spawn/peer assemblers)."""
    config = ctx.agent.config
    if getattr(config, "blackboards", None):
        tools = build_stage_tools(
            blackboards=config.blackboards,
            budget_ledger=ctx.budget_ledger,
        )
        attach_tools(active_tools, tool_schemas, tools)
    return spawn_acc
