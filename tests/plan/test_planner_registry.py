"""Planner × registry — a drafted step may name any registered unit.

The planner's node forms used to be a binary (tool / autonomous goal).
With the registry wired, a step can say ``"unit": "<name>"`` and run a
composed Sequential, a workflow, another agent — whatever composition
registered. The catalogue the model sees grows a units section; a step
naming a unit with no registry wired is skipped with a warning, not a
crash; an unknown name fails at resolution with the roster listed.
"""

from __future__ import annotations

import json
from typing import Any

from prodagent.kernel.registry import UnitRegistry
from prodagent.kernel.unit import Outcome, UnitContext, UnitMeta
from prodagent.plan.planner import Planner


class _Composed:
    """A named composed unit (stands in for Sequential(...)/a workflow)."""

    readonly = None
    kind = "composed"

    def __init__(self, answer: str = "composed ran") -> None:
        self.answer = answer

    @property
    def target(self) -> str:
        return "composed"

    async def run(self, input: Any, ctx: UnitContext) -> Outcome:
        return Outcome(value={"state": "completed", "output": self.answer})


def _planner(registry: UnitRegistry | None) -> Planner:
    return Planner.__new__(Planner) if False else Planner(None, registry=registry)  # type: ignore[arg-type]


def _draft(step: dict) -> str:
    return json.dumps({"steps": [step]})


class TestUnitSteps:
    def test_a_step_naming_a_registered_unit_resolves_to_it(self):
        registry = UnitRegistry()
        unit = _Composed()
        registry.register("triage", unit, UnitMeta(name="triage", description="triage the inbox"))
        planner = _planner(registry)

        nodes = planner._parse_nodes(_draft({"id": "s1", "unit": "triage", "depends_on": []}))
        assert len(nodes) == 1
        assert nodes[0].body is unit
        assert nodes[0].kind == "composed"

    def test_tool_and_goal_forms_still_work_alongside(self):
        registry = UnitRegistry()
        registry.register("triage", _Composed())
        planner = _planner(registry)

        nodes = planner._parse_nodes(
            json.dumps(
                {
                    "steps": [
                        {"id": "a", "action": "search", "depends_on": []},
                        {"id": "b", "goal": "find root cause", "depends_on": ["a"]},
                        {"id": "c", "unit": "triage", "depends_on": ["b"]},
                    ]
                }
            )
        )
        kinds = [n.kind for n in nodes]
        assert kinds == ["tool", "autonomous", "composed"]

    def test_a_unit_step_without_a_registry_is_skipped_not_fatal(self):
        planner = _planner(None)
        nodes = planner._parse_nodes(_draft({"id": "s1", "unit": "triage"}))
        assert nodes == []

    def test_an_unknown_unit_name_is_skipped_with_the_roster_logged(self):
        registry = UnitRegistry()
        registry.register("triage", _Composed())
        planner = _planner(registry)

        # malformed-node discipline applies: skipped with the roster in the
        # warning (one bad step never kills four good ones)
        nodes = planner._parse_nodes(_draft({"id": "s1", "unit": "ghost"}))
        assert nodes == []


class TestCatalogue:
    def test_the_model_sees_registered_units_in_the_catalogue(self):
        registry = UnitRegistry()
        registry.register("triage", _Composed(), UnitMeta(name="triage", description="triage it"))
        planner = _planner(registry)
        planner._tool_schemas = [{"name": "search", "input_schema": {}}]

        system = planner._build_system("", "PLAN")
        assert "Registered units" in system
        assert "triage" in system

    def test_no_registry_no_units_section(self):
        planner = _planner(None)
        system = planner._build_system("", "PLAN")
        assert "Registered units" not in system
