"""as_tool — the one delegation adapter: any Unit becomes a model-callable tool.

This is the whole multi-agent story in one function: an Agent, a composed
Sequential, an entire Workflow — wrap it, hand the tool to a parent, and
the parent's model delegates by name. There is no second mechanism and no
per-shape wrapper class.

Two execution paths behind one signature, by target kind:

- **An Agent** — full governance, unchanged from spawn: Crossing dispatch
  (dedupe + gate), the activation core through the RunnerPort (child runs
  as a real run with parentage and the chained ledger), contract admission
  on the result. The machinery is Spawn's, reused verbatim; ``as_tool``
  only rebuilds the roster around one named child.
- **Any other Unit** — the same budget envelope and admission pipeline,
  executed in-process: ``run_enveloped`` around ``unit.run`` (turn-slot
  accounting on the shared ledger), the result folded through the
  admission pipeline like a child result. No fork, no hop — a composed
  unit is library code, not a subordinate process.

``is_agentic`` rides the registry's ``UnitMeta`` when the unit is
registered; a bare unit defaults to agentic=True (unlabelled = expensive).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from prodagent.kernel.budget import run_enveloped
from prodagent.tooling.base import FunctionTool

if TYPE_CHECKING:
    from collections.abc import Callable

    from prodagent.coordination.messaging.contract import MessageContract
    from prodagent.kernel.budget import BudgetLedger
    from prodagent.kernel.unit import GraphUnit, Outcome, UnitContext

logger = logging.getLogger(__name__)

__all__ = ["as_tool", "unit_as_tool"]


def as_tool(
    target: Any,
    *,
    name: str | None = None,
    description: str = "",
    registry: Any | None = None,
    runner: Any | None = None,
    hooks: Any | None = None,
    framework_config: Any | None = None,
    constraints: list[str] | None = None,
    budget: Any | None = None,
    budget_ledger: BudgetLedger | None = None,
    parent_run_id: str | None = None,
    depth: int = 0,
    context_factory: Callable[[str], UnitContext] | None = None,
    contract: MessageContract | None = None,
) -> FunctionTool:
    """Wrap ``target`` (an Agent, a registry name, or any GraphUnit) as a Tool.

    Agents get the full spawn governance (the machinery is reused, not
    duplicated — ``coordination.spawn.Spawn``); other units execute
    in-process under the budget envelope and the admission pipeline. The
    ``spawn_<name>`` tool name of the old world is gone: the tool is named
    after the unit.
    """
    resolved_name, unit = _resolve_target(target, name, registry)

    if _is_agent(unit):
        return _agent_tool(
            unit,
            name=resolved_name,
            description=description,
            runner=runner,
            hooks=hooks,
            framework_config=framework_config,
            constraints=constraints,
            budget=budget,
            budget_ledger=budget_ledger,
            parent_run_id=parent_run_id,
            depth=depth,
        )
    if context_factory is None:
        raise ValueError(
            f"as_tool({resolved_name!r}): a non-Agent unit needs context_factory "
            "(a callable producing the UnitContext its run draws on)"
        )
    return unit_as_tool(
        unit,
        name=resolved_name,
        description=description,
        context_factory=context_factory,
        budget_ledger=budget_ledger,
        contract=contract,
        hooks=hooks,
    )


def unit_as_tool(
    unit: GraphUnit,
    *,
    name: str,
    description: str = "",
    context_factory: Callable[[str], UnitContext],
    budget_ledger: BudgetLedger | None = None,
    contract: MessageContract | None = None,
    hooks: Any | None = None,
) -> FunctionTool:
    """The in-process arm of :func:`as_tool` — any non-Agent Unit as a Tool.

    Budget: one reserved turn slot per call on the shared ledger (crash =
    slot consumed, same discipline as any enveloped member). Admission:
    the result crosses the UPSTREAM admission pipeline (contract when
    declared), like every agent-produced boundary payload."""
    from prodagent.coordination.messaging.envelope import Crossing, CrossingKind, Direction
    from prodagent.coordination.messaging.pipeline import admission_pipeline
    from prodagent.kernel.types import SideEffectLevel, ToolCall, ToolMeta

    async def _run(task: str) -> dict[str, Any]:
        ctx = context_factory(name)
        box: list[Outcome] = []

        async def _act() -> tuple[int, int, float]:
            # One reserved turn slot per call; a composed unit's tokens are
            # accounted where they burn (a child Agent hop keeps its own
            # ledger lines) — here the slot IS the unit's cost.
            box.append(
                await unit.run(
                    ToolCall(name=name, params={"task": task}, call_id=f"as_tool:{name}"), ctx
                )
            )
            return 1, 0, 0.0

        enveloped: tuple[int, int, float] | None = await run_enveloped(
            budget_ledger, member=name, act=_act
        )
        if enveloped is None:
            return {
                "state": "budget_exhausted",
                "output": f"unit {name!r} could not reserve a turn slot — chain budget is spent",
            }
        outcome = box[0]
        if isinstance(outcome.value, dict) and "state" in outcome.value:
            result = dict(outcome.value)
        else:
            result = {"state": "completed", "output": outcome.value}

        if contract is not None:
            delivery = await admission_pipeline(contract=contract, hooks=hooks).process(
                Crossing.mint(
                    direction=Direction.UPSTREAM,
                    kind=CrossingKind.RESULT,
                    from_agent=name,
                    to="caller",
                    payload=result,
                    trace_id=ctx.run_id,
                    message_id=f"{ctx.run_id}:{name}",
                )
            )
            if delivery.status == "rejected":
                return {**result, "state": "contract_violation", "output": delivery.reason}
        return result

    meta = ToolMeta(
        name=name,
        side_effect_level=SideEffectLevel.LOW,
        is_readonly=False,
        domain="orchestration",
    )
    schema = {
        "name": name,
        "description": description or f"Delegate a task to the unit {name!r}.",
        "input_schema": {
            "type": "object",
            "properties": {"task": {"type": "string", "description": "The task for the unit"}},
            "required": ["task"],
        },
    }
    return FunctionTool(name=name, fn=_run, meta=meta, schema=schema)


# ── resolution and the agent arm ───────────────────────────────────────────


def _resolve_target(target: Any, name: str | None, registry: Any | None) -> tuple[str, GraphUnit]:
    """A target is one of: a registry name (str), an Agent, or a bare unit.
    Returns (name, unit) with the name defaulted from the unit's roster
    identity."""
    if isinstance(target, str):
        if registry is None:
            raise ValueError(f"as_tool({target!r}): a name target needs the registry")
        return target, registry.require(target)
    unit_name = name or getattr(target, "name", None) or getattr(target, "target", "")
    if not unit_name:
        raise ValueError("as_tool: pass name= for units that carry no name of their own")
    return unit_name, target


def _is_agent(unit: Any) -> bool:
    return callable(getattr(unit, "spec", None)) and hasattr(unit, "chat_stream")


def _agent_tool(
    agent: Any,
    *,
    name: str,
    description: str,
    runner: Any | None,
    hooks: Any | None,
    framework_config: Any | None,
    constraints: list[str] | None,
    budget: Any | None,
    budget_ledger: BudgetLedger | None,
    parent_run_id: str | None,
    depth: int,
) -> FunctionTool:
    """The Agent arm: Spawn's governance reused around one child, the tool
    named after the agent (not a fixed ``spawn_agent``)."""
    from prodagent.coordination.spawn import Spawn
    from prodagent.kernel.types import SideEffectLevel, ToolMeta

    if runner is None:
        raise ValueError(f"as_tool({name!r}): an Agent target needs runner= (the hop's RunnerPort)")
    pipeline = Spawn(
        [agent],
        runner=runner,
        hooks=hooks,
        framework_config=framework_config,
        constraints=constraints,
        budget=budget,
        budget_ledger=budget_ledger,
        parent_run_id=parent_run_id,
        depth=depth,
    )
    schema = {
        "name": name,
        "description": description or pipeline.build_tool().schema.get("description", ""),
        "input_schema": pipeline.build_tool().schema["input_schema"],
    }
    meta = ToolMeta(
        name=name,
        side_effect_level=SideEffectLevel.LOW,
        is_readonly=True,
        enforced_idempotent=True,
        domain="orchestration",
    )

    async def _spawn(name_arg: str, task: str, input_refs: dict[str, str] | None = None) -> Any:
        # The schema's name field is the roster; the tool binds to one agent.
        return await pipeline.spawn(name, task, input_refs)

    return FunctionTool(name=name, fn=_spawn, meta=meta, schema=schema)
