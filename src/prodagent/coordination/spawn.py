"""Spawn — vertical sub-agent delegation (``agents=``)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any

from prodagent.coordination.messaging.contract import (
    DEFAULT_CHILD_CONTRACT,
    MessageContract,
)
from prodagent.kernel.budget import BudgetLedger
from prodagent.coordination.messaging.envelope import (
    Crossing,
    CrossingKind,
    Direction,
)
from prodagent.coordination.messaging.idempotency import IdempotentMessageHandler
from prodagent.coordination.messaging.packet import HandoffPacket
from prodagent.coordination.messaging.pipeline import (
    admission_pipeline,
    assembly_pipeline,
)
from prodagent.coordination.parent_runtime import ParentRuntime, describe_agent
from prodagent.core.error_reason import ErrorReason
from prodagent.core.exceptions import (
    SECURITY_VETO_EXCEPTIONS,
    BudgetExceeded,
)
from prodagent.core.types import (
    ErrorSeverity,
    RunState,
    SideEffectLevel,
    ToolError,
    ToolMeta,
)
from prodagent.hooks.events import HookEvent
from prodagent.runtime._tool_merge import attach_tools
from prodagent.tooling.base import FunctionTool

if TYPE_CHECKING:
    from prodagent.coordination.parent_runtime import SpawnAccumulator
    from prodagent.coordination.run_loop import RunContext
    from prodagent.core.config import FrameworkConfig
    from prodagent.hooks.registry import HookRegistry
    from prodagent.ports.dead_letter import DeadLetterStore
    from prodagent.ports.llm import LLMClient
    from prodagent.runtime.agent import Agent

logger = logging.getLogger(__name__)

STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_SUSPENDED = "suspended"
STATE_RUNNING = "running"
STATE_TIMEOUT = "timeout"
STATE_DUPLICATE = "duplicate"
STATE_CONTRACT_VIOLATION = "contract_violation"
STATE_HANDOFF_REJECTED = "handoff_rejected"


@dataclass
class ChildResult:
    """Structured result of a child agent run."""

    agent: str
    state: str
    output: str = ""
    turns: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_history: list[Any] = field(default_factory=list)
    approval_request_id: str = ""
    failed_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def short_result(
    name: str, state: str, message: str, *, failed_reason: str | None = None
) -> ChildResult:
    return ChildResult(agent=name, state=state, output=message, failed_reason=failed_reason)


def build_spawn_tool_schema(agents: list[Agent]) -> dict[str, Any]:
    agent_lines = "\n".join(f"  - {a.name}: {describe_agent(a)}" for a in agents)
    return {
        "name": "spawn_agent",
        "description": (
            "Delegate a sub-task to a specialised sub-agent and return its result.\n"
            "The sub-agent runs independently with an isolated context window.\n"
            "You may call this multiple times in one turn to run sub-agents in "
            "parallel; the framework fans them out concurrently.\n\n"
            f"Available sub-agents:\n{agent_lines}"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Sub-agent identifier",
                    "enum": [a.name for a in agents],
                },
                "task": {
                    "type": "string",
                    "description": "Specific task instruction for the sub-agent",
                },
                "input_refs": {
                    "type": "object",
                    "description": (
                        "References (not content) the sub-agent resolves via its "
                        'tools — e.g. {"order_record": "orders/123"}. Pass '
                        "handles instead of inlining bulk payloads to save the "
                        "child's context window and enforce physical isolation."
                    ),
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["name", "task"],
        },
    }


@dataclass
class SpawnTool:
    """The spawn_agent tool + its shared accumulator, built from child Agent specs."""

    tool: FunctionTool
    accumulator: SpawnAccumulator


class Spawn:
    """Runs a child agent end-to-end: packet → timeout → security → fold.

    Backs ``agents=`` (vertical delegation): parent calls ``spawn_agent``,
    waits synchronously for the child to finish, gets a structured
    ``ChildResult`` back while its own run continues. Contrast with
    :class:`~prodagent.coordination.peer.Peer` (``peers=``, horizontal
    hand-off): parent's run *ends*, control transfers to the peer instead of
    returning a result."""

    def __init__(
        self,
        agents: list[Agent],
        *,
        llm: LLMClient,
        hooks: HookRegistry | None,
        framework_config: FrameworkConfig | None,
        ctx: ParentRuntime,
        dead_letter_queue: DeadLetterStore | None = None,
    ) -> None:
        from prodagent.backends.factory import resolve_dead_letter
        from prodagent.core.config import FrameworkConfig

        self._spec_map = {a.name: a for a in agents}
        self._llm = llm
        self._hooks = hooks
        self._framework_config = framework_config or FrameworkConfig.from_env()
        self._ctx = ctx
        orch = self._framework_config.orchestration
        self._handoff_output_max_chars = orch.handoff_output_max_chars
        self._default_timeout_s = orch.spawn_default_timeout_s
        self._dlq: DeadLetterStore = (
            dead_letter_queue
            if dead_letter_queue is not None
            else resolve_dead_letter(self._framework_config)
        )
        # Shared chain ledger (one per RunLoop) — peers and siblings see the same
        # spend. A standalone Spawn (no chain) still enforces its own ceiling.
        self._budget_ledger = ctx.budget_ledger or (
            BudgetLedger(max=ctx.budget) if ctx.budget is not None else None
        )
        self._idempotency = IdempotentMessageHandler(ttl_seconds=orch.handoff_idempotency_ttl_s)
        self._dispatch_pipeline = assembly_pipeline(
            dedupe=self._idempotency,
            hooks=self._hooks,
            dead_letter=self.dlq,
        )
        self._result_pipeline = admission_pipeline(
            contract=self._contract_for,
            trim=self._bound_result,
            hooks=self._hooks,
            dead_letter=self.dlq,
            audit_event=self._result_audit_event,
            max_chars=self._handoff_output_max_chars,
        )

    def _contract_for(self, crossing: Crossing[Any]) -> MessageContract | None:
        """Resolve each child's declared output contract from its result."""
        payload = crossing.payload
        agent = payload.get("agent", "") if isinstance(payload, Mapping) else ""
        spec = self._spec_map.get(agent)
        if spec is not None and spec.config.output_contract is not None:
            return spec.config.output_contract
        return DEFAULT_CHILD_CONTRACT

    def _bound_result(self, payload: Any) -> Any:
        """Cap the child's free-text output — one knob bounds every
        agent-produced string crossing any boundary."""
        if isinstance(payload, Mapping) and isinstance(payload.get("output"), str):
            return {**payload, "output": payload["output"][: self._handoff_output_max_chars]}
        return payload

    def _result_audit_event(
        self, crossing: Crossing[Any]
    ) -> tuple[HookEvent, dict[str, Any]] | None:
        payload = crossing.payload if isinstance(crossing.payload, Mapping) else {}
        return (
            HookEvent.AGENT_RESULT,
            {
                "name": payload.get("agent", crossing.from_agent),
                "state": payload.get("state", ""),
                "turns": crossing.meta.get("turns", 0),
                "output": str(payload.get("output", ""))[:120],
                "depth": crossing.meta.get("depth", 0),
                "parent_run_id": crossing.meta.get("parent_run_id"),
                "child_run_id": crossing.meta.get("child_run_id"),
            },
        )

    @property
    def accumulator(self) -> SpawnAccumulator:
        return self._ctx.accumulator

    @property
    def dlq(self) -> DeadLetterStore:
        return self._dlq

    async def spawn(
        self,
        name: str,
        task: str,
        input_refs: dict[str, str] | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        spec = self._spec_map.get(name)
        if spec is None:
            return ToolError.from_reason(
                ErrorReason.TOOL_NOT_AVAILABLE,
                code="subagent_not_found",
                message=f"Unknown sub-agent {name!r}. Available: {list(self._spec_map.keys())}",
            ).as_dict()

        packet_kwargs: dict[str, Any] = {
            "task_description": task,
            "constraints": list(self._ctx.constraints),
            "available_tools": [t.name for t in spec.inline_tools],
            "input_refs": input_refs or {},
        }
        if idempotency_key:
            packet_kwargs["message_id"] = idempotency_key
        packet = HandoffPacket(**packet_kwargs)
        logger.info(
            "HandoffPacket[%s]: agent=%s  tools=%s  task=%s",
            packet.task_id[:8],
            name,
            packet.available_tools,
            task[:60],
        )

        from prodagent.core.state.run import child_run_id as _child_run_id

        child_run_id = (
            _child_run_id(self._ctx.parent_run_id, name) if self._ctx.parent_run_id else None
        )

        # DOWNSTREAM crossing: dispatch the packet to the child through the
        # assembly pipeline (dedupe + gate). Rejected dispatches die before
        # the child burns any budget.
        dispatch = await self._dispatch_pipeline.process(
            Crossing.mint(
                direction=Direction.DOWNSTREAM,
                kind=CrossingKind.DISPATCH,
                from_agent="parent",
                to=name,
                payload=packet,
                trace_id=self._ctx.parent_run_id or "",
                message_id=packet.message_id,
                depth=self._ctx.depth + 1,
                child_run_id=child_run_id or "",
            )
        )
        if dispatch.status == "duplicate":
            logger.warning(
                "IdempotentHandler[%s]: duplicate suppressed (agent=%s)",
                packet.message_id[:8],
                name,
            )
            return short_result(
                name,
                STATE_DUPLICATE,
                "Duplicate request suppressed by idempotency layer",
            ).as_dict()
        if dispatch.status == "rejected":
            return short_result(
                name,
                STATE_HANDOFF_REJECTED,
                f"Dispatch rejected: {dispatch.reason}",
            ).as_dict()

        await self._fire(
            HookEvent.AGENT_SPAWN,
            name=name,
            task=task,
            packet_id=packet.task_id,
            depth=self._ctx.depth + 1,
            parent_run_id=self._ctx.parent_run_id,
            child_run_id=child_run_id,
        )

        if self._budget_ledger is not None:
            try:
                await self._budget_ledger.reserve(member=name, turns=1)
            except BudgetExceeded as exc:
                return ToolError.from_reason(
                    ErrorReason.BUDGET_EXCEEDED,
                    code="spawn_budget_exhausted",
                    message=f"Cannot spawn {name!r}: {exc.message}",
                    hint="Concurrent sub-agent spend has hit the shared budget ceiling.",
                    severity=ErrorSeverity.RED,
                ).as_dict()

        result = await self._run_with_timeout(spec, task, packet, child_run_id)

        if self._budget_ledger is not None:
            await self._budget_ledger.commit(
                member=name,
                turns=result.turns,
                tokens=result.input_tokens + result.output_tokens,
                cost_usd=result.cost_usd,
                reserved_turns=1,
            )

        if result.state == STATE_TIMEOUT:
            return ToolError.from_reason(
                ErrorReason.TIMEOUT,
                code="subagent_timeout",
                message=f"Sub-agent {name!r} timed out after {result.output}",
                hint="The child ran past its wall-clock budget. Do not retry the same spawn — investigate the child's plan or raise its budget.",
                severity=ErrorSeverity.RED,
            ).as_dict()

        if result.state == STATE_SUSPENDED and result.approval_request_id:
            self._ctx.accumulator.add(result)
            logger.info(
                "[spawn] child %r suspended pending approval (request_id=%s) — "
                "propagating to parent run",
                name,
                result.approval_request_id,
            )
            return {
                "suspended": True,
                "reason": f"sub-agent {name!r} suspended pending approval",
                "tool": "spawn_agent",
                "approval_request_id": result.approval_request_id,
                "agent": name,
            }

        self._ctx.accumulator.add(result)

        # UPSTREAM crossing: the child's result enters the parent's world
        # through the admission pipeline (per-spec contract → trim → security
        # gate → audit). Same message_id as the dispatch — one logical
        # crossing, two directions. Rejections are dead-lettered at the
        # pipeline boundary; strict contract violations never reach the parent.
        delivery = await self._result_pipeline.process(
            Crossing.mint(
                direction=Direction.UPSTREAM,
                kind=CrossingKind.RESULT,
                from_agent=name,
                to="parent",
                payload=result.as_dict(),
                trace_id=self._ctx.parent_run_id or "",
                message_id=packet.message_id,
                depth=self._ctx.depth + 1,
                parent_run_id=self._ctx.parent_run_id,
                child_run_id=child_run_id or "",
                turns=result.turns,
            )
        )
        if delivery.status == "rejected":
            if delivery.stage == "gate":
                return short_result(
                    name,
                    STATE_HANDOFF_REJECTED,
                    f"Handoff rejected by security policy: {delivery.reason}",
                ).as_dict()
            return short_result(
                name,
                STATE_CONTRACT_VIOLATION,
                f"Child result rejected by contract: {delivery.reason}",
            ).as_dict()

        # What the parent's context receives: the contract-whitelisted view
        # plus the four accounting scalars (budget facts the whitelist doesn't
        # know). tool_history and other internal fields never cross.
        admitted = delivery.crossing.payload
        return {
            **admitted,
            "turns": result.turns,
            "cost_usd": result.cost_usd,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        }

    async def _run_with_timeout(
        self, spec: Agent, task: str, packet: HandoffPacket, child_run_id: str | None
    ) -> ChildResult:
        timeout = (
            spec.budget_config.max_seconds
            if spec.budget_config is not None
            else self._default_timeout_s
        )
        try:
            return await asyncio.wait_for(
                self._run_child(spec, task, packet, child_run_id),
                timeout=timeout,
            )
        except TimeoutError:
            return short_result(
                spec.name, STATE_TIMEOUT, f"Sub-agent timed out after {timeout:.0f}s"
            )
        except SECURITY_VETO_EXCEPTIONS:
            raise
        except Exception as exc:
            logger.error("Sub-agent %r failed: %s", spec.name, exc, exc_info=True)
            return short_result(spec.name, STATE_FAILED, str(exc), failed_reason="raised")

    async def _run_child(
        self, spec: Agent, task: str, packet: HandoffPacket, child_run_id: str | None
    ) -> ChildResult:
        ctx = self._ctx
        child_task = packet.to_task_prompt()
        inherited_hooks = self._hooks or spec.hooks
        runtime = replace(
            ctx,
            llm=self._llm,
            hooks=inherited_hooks,
            framework_config=self._framework_config,
            budget=ctx.budget or spec.budget_config,
        )
        child = spec.fork_as_spawn(runtime)

        try:
            from prodagent.coordination.run_loop import drive

            run = await drive(
                child,
                child_task,
                run_id=child_run_id,
                parent_run_id=self._ctx.parent_run_id,
            )
        except SECURITY_VETO_EXCEPTIONS:
            raise
        except Exception as exc:
            logger.error("Sub-agent %r failed: %s", spec.name, exc)
            return short_result(spec.name, STATE_FAILED, str(exc), failed_reason="raised")

        if run is None:
            return short_result(
                spec.name,
                STATE_FAILED,
                "child run produced no terminal result",
                failed_reason="failed",
            )

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

    async def _fire(self, event: HookEvent, **fields: Any) -> None:
        if self._hooks:
            await self._hooks.fire(event, **fields)

    def build_tool(self) -> FunctionTool:
        schema = build_spawn_tool_schema(list(self._spec_map.values()))
        meta = ToolMeta(
            name="spawn_agent",
            side_effect_level=SideEffectLevel.LOW,
            is_readonly=True,
            enforced_idempotent=True,
            domain="orchestration",
            timeout_seconds=self._framework_config.orchestration.spawn_tool_timeout_ms / 1_000,
        )
        return FunctionTool(name="spawn_agent", fn=self.spawn, meta=meta, schema=schema)


def build_spawn_tools_for_agent(
    agents: list[Agent],
    *,
    llm: LLMClient,
    hooks: HookRegistry | None = None,
    framework_config: FrameworkConfig | None = None,
    context: ParentRuntime | None = None,
    dead_letter_queue: DeadLetterStore | None = None,
) -> SpawnTool | None:
    if not agents:
        return None

    ctx = context or ParentRuntime()
    pipeline = Spawn(
        agents,
        llm=llm,
        hooks=hooks,
        framework_config=framework_config,
        ctx=ctx,
        dead_letter_queue=dead_letter_queue,
    )
    return SpawnTool(tool=pipeline.build_tool(), accumulator=ctx.accumulator)


def assemble_spawn_tools(
    ctx: RunContext,
    active_tools: list[Any],
    tool_schemas: list[dict[str, Any]],
) -> SpawnAccumulator | None:
    """Build spawn tools for ``agent.child_agents`` and append them to ``active_tools``/``tool_schemas``."""
    agent = ctx.agent
    if not agent.child_agents:
        return None
    spawn_tools = build_spawn_tools_for_agent(
        agent.child_agents,
        llm=ctx.llm,
        hooks=agent.hooks,
        framework_config=agent.framework_config,
        context=ParentRuntime.from_context(ctx),
    )
    if spawn_tools is None:
        return None
    attach_tools(active_tools, tool_schemas, [spawn_tools.tool])
    return spawn_tools.accumulator
