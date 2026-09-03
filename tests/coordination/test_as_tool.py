"""as_tool — the one delegation adapter, over both arms.

Laws under test: any GraphUnit wraps as a tool and runs in-process under
the budget envelope (crash = slot consumed); an Agent target reuses
Spawn's governance with the tool named after the agent; a name target
resolves through the UnitRegistry; a non-unit call fails loudly with the
fix named.
"""

from __future__ import annotations

from typing import Any

import pytest

from prodagent.coordination.as_tool import as_tool, unit_as_tool
from prodagent.kernel.registry import UnitRegistry
from prodagent.kernel.unit import Outcome, UnitContext, UnitMeta


class _Fixed:
    """A unit that always answers the same dict."""

    readonly = True
    kind = "fixed"

    def __init__(self, answer: dict) -> None:
        self.answer = answer

    @property
    def target(self) -> str:
        return "fixed"

    async def run(self, input: Any, ctx: UnitContext) -> Outcome:
        return Outcome(value=self.answer)


def _ctx_factory(run_id: str) -> UnitContext:
    return UnitContext(run_id=run_id or "r-as-tool")


class TestUnitArm:
    async def test_a_bare_unit_runs_and_folds_its_value(self):
        tool = as_tool(
            _Fixed({"verdict": "clean"}),
            name="auditor",
            context_factory=_ctx_factory,
        )
        raw = await tool(task="audit the ledger")
        assert raw.value["state"] == "completed"
        assert raw.value["output"] == {"verdict": "clean"}

    async def test_a_returning_dict_with_state_passes_through(self):
        tool = as_tool(
            _Fixed({"state": "completed", "output": "found 3 issues"}),
            name="scout",
            context_factory=_ctx_factory,
        )
        raw = await tool(task="dig")
        assert raw.value["state"] == "completed"
        assert raw.value["output"] == "found 3 issues"

    async def test_budget_envelope_reserves_and_consumes_a_slot(self):
        from prodagent.kernel.budget import BudgetLedger, HardBudget

        ledger = BudgetLedger(max=HardBudget(max_turns=1))
        tool = as_tool(
            _Fixed({"ok": True}), name="metered", context_factory=_ctx_factory, budget_ledger=ledger
        )
        first_raw = await tool(task="run once")
        assert first_raw.value["state"] == "completed"
        second_raw = await tool(task="run again — the chain's only slot is spent")
        assert second_raw.value["state"] == "budget_exhausted"

    async def test_the_tool_is_named_after_the_unit(self):
        tool = unit_as_tool(_Fixed({}), name="auditor", context_factory=_ctx_factory)
        assert tool.name == "auditor"
        assert tool.schema["name"] == "auditor"


class TestRegistryArm:
    async def test_a_name_target_resolves_through_the_registry(self):
        registry = UnitRegistry()
        registry.register("auditor", _Fixed({"verdict": "clean"}), UnitMeta(name="auditor"))
        tool = as_tool("auditor", registry=registry, context_factory=_ctx_factory)
        raw = await tool(task="audit")
        assert raw.value["output"] == {"verdict": "clean"}

    def test_an_unknown_name_names_every_known_unit(self):
        registry = UnitRegistry()
        registry.register("auditor", _Fixed({}))
        with pytest.raises(KeyError, match="Known units.*auditor"):
            as_tool("ghost", registry=registry, context_factory=_ctx_factory)

    def test_a_name_target_without_a_registry_says_so(self):
        with pytest.raises(ValueError, match="needs the registry"):
            as_tool("auditor", context_factory=_ctx_factory)


class TestGuardrails:
    async def test_a_non_agent_unit_without_context_factory_fails_loudly(self):
        with pytest.raises(ValueError, match="context_factory"):
            as_tool(_Fixed({}), name="orphan")

    async def test_an_agent_target_without_runner_fails_loudly(self):
        class _FakeAgent:
            name = "child"

            def spec(self): ...
            async def chat_stream(self, *a, **k): ...

        with pytest.raises(ValueError, match="runner="):
            as_tool(_FakeAgent())  # type: ignore[arg-type]


class TestHandoffEscape:
    async def test_handoff_control_visible_on_outcome(self):
        # The escape contract itself lives in kernel.unit (HANDOFF_ESCAPED);
        # here we pin that a unit returning control=Handoff surfaces its
        # sentinel value through the tool boundary — the caller reads
        # "control left through me", not a half-result.
        from prodagent.kernel.unit import HANDOFF_ESCAPED, Handoff, Outcome

        class _Escaper:
            readonly = False
            kind = "escaper"

            @property
            def target(self):
                return "escaper"

            async def run(self, input: Any, ctx: UnitContext) -> Outcome:
                return Outcome(value=HANDOFF_ESCAPED, control=Handoff(target=self))

        tool = unit_as_tool(_Escaper(), name="escaper", context_factory=_ctx_factory)
        raw = await tool(task="take over")
        assert raw.value["state"] == "completed"
        assert raw.value["output"] == HANDOFF_ESCAPED
