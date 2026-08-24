"""Planner — one LLM call to produce a plan DAG, one to replan on failure."""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING, Any

from prodagent.core.exceptions import SECURITY_VETO_EXCEPTIONS, LLMError
from prodagent.kernel.types import StepStatus
from prodagent.hooks import fire as _fire
from prodagent.kernel.bus import HookEvent
from prodagent.llm import LLMConfig
from prodagent.llm.structured_output import extract_json_object
from prodagent.plan.dag import Plan, PlanStep

if TYPE_CHECKING:
    from prodagent.kernel.state import AgentRun
    from prodagent.kernel.types import MessageList
    from prodagent.kernel.bus import HookRegistry
    from prodagent.llm import LLMClient

logger = logging.getLogger(__name__)

_DEFAULT_PLANNING_MAX_TOKENS = 16_384


@dataclass
class PlanDraft:
    """Parsed Plan (or None) + raw response text from one planning LLM call."""

    plan: Plan | None
    raw_text: str


def _load_prompt(name: str) -> str:
    return (resources.files("prodagent.plan.prompts") / f"{name}.txt").read_text()


def _tool_reference(tool_schemas: list[dict[str, Any]]) -> str:
    """The only tool catalogue the planner sees."""
    compact = [
        {"name": s.get("name", "?"), "input_schema": s.get("input_schema", {})}
        for s in tool_schemas
    ]
    return "Available tools (use ONLY these):\n" + json.dumps(compact, indent=2)


def _replan_user_prompt(plan: Plan, failed_step: PlanStep, error: str) -> str:
    completed = (
        "\n".join(
            f"  {s.step_id}: {s.action} → COMPLETED"
            for s in plan.steps
            if s.status is StepStatus.COMPLETED
        )
        or "  (none)"
    )
    obsolete = (
        ", ".join(s.step_id for s in plan.steps if s.status is StepStatus.OBSOLETE) or "(none)"
    )
    return (
        f"Step '{failed_step.step_id}' ({failed_step.action}) failed.\n"
        f"Error: {error}\n\n"
        f"Completed steps (do not repeat):\n{completed}\n"
        f"Obsoleted steps: {obsolete}\n\n"
        "Propose replacement steps to recover from this failure."
    )


class Planner:
    """LLM call failure raises ``LLMError``; parse failure returns ``None`` (generate) / ``[]`` (replan)."""

    def __init__(
        self,
        llm: LLMClient,
        config: LLMConfig | None,
        tool_schemas: list[dict[str, Any]] | None,
        *,
        hooks: HookRegistry | None = None,
        framework_config: Any = None,
    ) -> None:
        self._llm = llm
        self._config = config
        self._tool_schemas = tool_schemas or []
        self._hooks = hooks
        if framework_config is not None:
            self._max_tokens = framework_config.orchestration.planning_max_tokens
        else:
            self._max_tokens = _DEFAULT_PLANNING_MAX_TOKENS
        self._planning_system = _load_prompt("planning")
        self._replan_system = _load_prompt("replan")

    async def generate(
        self,
        task: str,
        system: str,
        messages: MessageList,
        run: AgentRun,
    ) -> PlanDraft:
        messages = list(messages) + [{"role": "user", "content": task}]
        raw = await self._call_llm(messages, self._build_system(system, self._planning_system), run)
        return PlanDraft(plan=self._parse_plan(raw), raw_text=raw)

    async def replan(
        self,
        plan: Plan,
        failed_step: PlanStep,
        error: str,
        system: str,
        original_messages: MessageList,
        run: AgentRun,
    ) -> list[PlanStep]:
        clean: MessageList = []
        for m in original_messages:
            role = m.get("role", "") if isinstance(m, dict) else getattr(m, "role", "")
            if role in ("user", "assistant"):
                clean.append(m)
        clean.append({"role": "user", "content": _replan_user_prompt(plan, failed_step, error)})
        raw = await self._call_llm(clean, self._build_system(system, self._replan_system), run)
        return self._parse_steps(raw)

    def _build_system(self, caller_system: str, prompt: str) -> str:
        parts = [caller_system, prompt]
        if self._tool_schemas:
            parts.append(_tool_reference(self._tool_schemas))
        return "\n\n".join(parts)

    def _planning_cfg(self) -> LLMConfig:
        if self._config is not None:
            return dataclasses.replace(self._config, max_tokens=self._max_tokens)
        return LLMConfig(max_tokens=self._max_tokens)

    async def _call_llm(
        self,
        messages: MessageList,
        system: str,
        run: AgentRun,
    ) -> str:
        await _fire(
            self._hooks,
            HookEvent.LLM_REQUEST,
            system=system[:200],
            system_len=len(system),
            messages=messages,
            msg_count=len(messages),
            phase="planning",
            run_id=run.run_id,
        )

        async def _on_chunk(text: str) -> None:
            collected.append(text)
            await _fire(self._hooks, HookEvent.THINK, text=text, run_id=run.run_id)

        collected: list[str] = []

        try:
            response = await self._llm.complete(
                messages,
                system=system,
                config=self._planning_cfg(),
                on_chunk=_on_chunk,
            )
        except SECURITY_VETO_EXCEPTIONS:
            raise
        except Exception as exc:
            logger.error("[Plan] LLM call failed: %s", exc)
            raise LLMError(f"planning LLM call failed: {exc}") from exc
        run.metrics.turn_count += 1
        if not getattr(response, "from_cache", False):
            run.add_tokens(response, cost_usd=self._planning_cfg().cost_for_response(response))
        return response.content or "".join(collected)

    def _parse_plan(self, content: str) -> Plan | None:
        steps = self._parse_steps(content)
        if not steps:
            return None
        plan = Plan()
        plan.add_steps(steps)
        return plan

    @staticmethod
    def _parse_steps(content: str) -> list[PlanStep]:
        """Tolerates markdown fences + surrounding prose."""
        try:
            data = json.loads(extract_json_object(content))
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("[Plan] JSON parse failed: %s\ncontent=%s", exc, content[:200])
            return []
        steps: list[PlanStep] = []
        for s in data.get("steps", []):
            try:
                steps.append(
                    PlanStep(
                        step_id=s["id"],
                        action=s["action"],
                        params=s.get("params", {}),
                        depends_on=s.get("depends_on", []),
                        is_terminal=bool(s.get("terminal", False)),
                        replaces_step_id=s.get("replaces"),
                    )
                )
            except KeyError as exc:
                logger.warning("[Plan] step missing required field %s in: %s", exc, s)
                continue
        return steps
