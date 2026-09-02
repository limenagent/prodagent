"""Planner — one LLM call to produce a plan DAG, one to replan on failure."""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING, Any

from prodagent.base.errors import SECURITY_VETO_EXCEPTIONS, LLMError
from prodagent.hooks import fire as _fire
from prodagent.kernel.bodies.base import ReActBody, ToolBody
from prodagent.kernel.bus import HookEvent
from prodagent.kernel.types import NodeStatus
from prodagent.llm import LLMConfig
from prodagent.llm.structured_output import extract_json_object
from prodagent.plan.dag import Node, Plan

if TYPE_CHECKING:
    from collections.abc import Mapping

    from prodagent.kernel.bus import HookRegistry
    from prodagent.kernel.node_state import NodeRuntimeState
    from prodagent.kernel.state import AgentRun
    from prodagent.kernel.types import MessageList
    from prodagent.llm import LLMClient

logger = logging.getLogger(__name__)

_DEFAULT_PLANNING_MAX_TOKENS = 16_384


@dataclass
class PlanDraft:
    """Parsed nodes (empty means no plan) + raw response text from one
    planning LLM call — parse success and raw evidence travel together so
    a bad draft is auditable against what the model actually said."""

    nodes: list[Node]
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


def _replan_user_prompt(
    plan: Plan, failed_node: Node, error: str, states: Mapping[str, NodeRuntimeState]
) -> str:
    """The recovery prompt's core: failure, its error, and — critically —
    the completed/obsolete census. "Do not repeat" is not a courtesy; a
    model that re-proposes a completed node would re-fire its side effects
    at merge time."""
    completed = (
        "\n".join(
            f"  {n.node_id}: {n.action} → COMPLETED"
            for n in plan.nodes.values()
            if states.get(n.node_id) is not None
            and states[n.node_id].status is NodeStatus.COMPLETED
        )
        or "  (none)"
    )
    obsolete = (
        ", ".join(
            n.node_id
            for n in plan.nodes.values()
            if states.get(n.node_id) is not None and states[n.node_id].status is NodeStatus.OBSOLETE
        )
        or "(none)"
    )
    return (
        f"Node '{failed_node.node_id}' ({failed_node.action}) failed.\n"
        f"Error: {error}\n\n"
        f"Completed nodes (do not repeat):\n{completed}\n"
        f"Obsoleted nodes: {obsolete}\n\n"
        "Propose replacement nodes to recover from this failure."
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
        """One planning call. Returns the draft *with raw text* — parse
        success and raw evidence travel together so a bad plan is auditable
        against what the model actually said."""
        messages = list(messages) + [{"role": "user", "content": task}]
        raw = await self._call_llm(messages, self._build_system(system, self._planning_system), run)
        return PlanDraft(nodes=self._parse_nodes(raw), raw_text=raw)

    async def repair(
        self,
        draft: PlanDraft,
        issues: str,
        task: str,
        system: str,
        run: AgentRun,
    ) -> PlanDraft:
        """One repair round: the rejected draft and its validator issues go
        back to the model — errors are feedback, not exceptions (column 16)."""
        messages: MessageList = [
            {"role": "assistant", "content": draft.raw_text},
            {
                "role": "user",
                "content": f"That plan was rejected:\n{issues}\n\n"
                f"Return a corrected plan as JSON for this task:\n{task}",
            },
        ]
        raw = await self._call_llm(messages, self._build_system(system, self._planning_system), run)
        return PlanDraft(nodes=self._parse_nodes(raw), raw_text=raw)

    async def replan(
        self,
        plan: Plan,
        failed_node: Node,
        error: str,
        system: str,
        original_messages: MessageList,
        run: AgentRun,
    ) -> list[Node]:
        """One recovery call — replacement steps only, never a full re-plan.
        The prompt shows what survived (completed), what died (obsolete), and
        what broke; ``Plan.merge`` then re-links the lineage."""
        clean: MessageList = []
        # Replanning needs the narrative, not the raw tool dumps — strip tool
        # turns so the recovery prompt stays about what happened, not payloads.
        for m in original_messages:
            role = m.get("role", "") if isinstance(m, dict) else getattr(m, "role", "")
            if role in ("user", "assistant"):
                clean.append(m)
        clean.append(
            {
                "role": "user",
                "content": _replan_user_prompt(plan, failed_node, error, run.node_states),
            }
        )
        raw = await self._call_llm(clean, self._build_system(system, self._replan_system), run)
        return self._parse_nodes(raw)

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

    def _parse_nodes(self, content: str) -> list[Node]:
        """Tolerates markdown fences + surrounding prose. Two node forms —
        column 7's two schools: ``action`` pins the tool (execution has
        zero model calls); ``goal`` declares an autonomous node that works
        out its own calls (use sparingly: it costs more)."""
        try:
            data = json.loads(extract_json_object(content))
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("[Plan] JSON parse failed: %s\ncontent=%s", exc, content[:200])
            return []
        nodes: list[Node] = []
        for s in data.get("steps", []):
            try:
                if s.get("goal"):
                    nodes.append(
                        Node(
                            node_id=s["id"],
                            body=ReActBody(goal=str(s["goal"])),
                            params={},
                            depends_on=s.get("depends_on", []),
                            is_terminal=bool(s.get("terminal", False)),
                            replaces_node_id=s.get("replaces"),
                        )
                    )
                else:
                    nodes.append(
                        Node(
                            node_id=s["id"],
                            body=ToolBody(s["action"]),
                            params=s.get("params", {}),
                            depends_on=s.get("depends_on", []),
                            is_terminal=bool(s.get("terminal", False)),
                            replaces_node_id=s.get("replaces"),
                        )
                    )
            except KeyError as exc:
                # A malformed node is skipped, not fatal — four good nodes and
                # one bad field still make an executable plan.
                logger.warning("[Plan] node missing required field %s in: %s", exc, s)
                continue
        return nodes
