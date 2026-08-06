"""SpawnPipeline — vertical sub-agent delegation (``agents=``)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any

from prodagent.backends.factory import resolve_dead_letter
from prodagent.core.error_reason import ErrorReason
from prodagent.core.exceptions import SECURITY_VETO_EXCEPTIONS, ContractViolationError
from prodagent.core.types import (
    ErrorSeverity,
    RunState,
    SideEffectLevel,
    ToolError,
    ToolMeta,
)
from prodagent.hooks.checkpoint import CheckPoint
from prodagent.hooks.events import HookEvent
from prodagent.runtime._tool_merge import attach_tools
from prodagent.runtime.coordination.handoff import (
    HandoffContract,
    HandoffInterceptor,
    HandoffPacket,
)
from prodagent.runtime.coordination.idempotency import IdempotentMessageHandler
from prodagent.runtime.coordination.parent_runtime import ParentRuntime, describe_agent
from prodagent.tooling.base import FunctionTool

if TYPE_CHECKING:
    from prodagent.core.config import FrameworkConfig
    from prodagent.hooks.registry import HookRegistry
    from prodagent.ports.dead_letter import DeadLetterStore
    from prodagent.ports.llm import LLMClient
    from prodagent.runtime.agent import Agent
    from prodagent.runtime.coordination.accounting import SpawnAccumulator
    from prodagent.runtime.run_context import RunContext

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


class SpawnPipeline:
    """Runs a child agent end-to-end: packet → timeout → security → fold.

    Backs the ``agents=`` keyword (vertical delegation): the parent calls
    ``spawn_agent``, waits synchronously for the child to finish, and gets a
    structured ``ChildResult`` back while its own run continues. Contrast with
    :class:`~prodagent.runtime.coordination.peer.PeerPipeline`, which backs
    ``peers=`` (horizontal hand-off): the parent's run *ends* and control
    transfers to the peer instead of returning a result.
    """

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
        from prodagent.core.config import FrameworkConfig

        self._spec_map = {a.name: a for a in agents}
        self._llm = llm
        self._hooks = hooks
        self._framework_config = framework_config or FrameworkConfig.from_env()
        self._ctx = ctx
        orch = self._framework_config.orchestration
        self._idempotency = IdempotentMessageHandler(ttl_seconds=orch.spawn_idempotency_ttl_s)
        self._handoff_output_max_chars = orch.spawn_handoff_output_max_chars
        self._default_timeout_s = orch.spawn_default_timeout_s
        self._interceptor = HandoffInterceptor()
        self._dlq: DeadLetterStore | None = dead_letter_queue
        self._default_contract = HandoffContract(
            required_fields=["output", "state"],
            field_types={"output": str, "state": str},
            strict=True,
        )

    @property
    def accumulator(self) -> SpawnAccumulator:
        return self._ctx.accumulator

    @property
    def dlq(self) -> DeadLetterStore:
        if self._dlq is None:
            self._dlq = resolve_dead_letter(self._framework_config)
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

        if await self._idempotency.is_duplicate(packet.message_id):
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

        from prodagent.core.state.run import child_run_id as _child_run_id

        child_run_id = (
            _child_run_id(self._ctx.parent_run_id, name) if self._ctx.parent_run_id else None
        )
        await self._fire(
            HookEvent.AGENT_SPAWN,
            name=name,
            task=task,
            packet_id=packet.task_id,
            depth=self._ctx.depth + 1,
            parent_run_id=self._ctx.parent_run_id,
            child_run_id=child_run_id,
        )

        result = await self._run_with_timeout(spec, task, packet, child_run_id)

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

        contract = spec.config.output_contract or self._default_contract
        try:
            self._interceptor.intercept(result.as_dict(), contract)
        except ContractViolationError as exc:
            dlq_state = self.dlq.on_failure(packet.message_id, result.as_dict(), str(exc))
            logger.warning(
                "HandoffInterceptor[%s]: contract violation (%s) → %s",
                packet.task_id[:8],
                exc.message[:80],
                dlq_state,
            )
            if contract.strict:
                return short_result(
                    name,
                    STATE_CONTRACT_VIOLATION,
                    f"Child result rejected by contract: {exc}",
                ).as_dict()

        rejected = await self._check_handoff_security(packet, result, name)
        if rejected is not None:
            return rejected.as_dict()

        await self._fire(
            HookEvent.AGENT_RESULT,
            name=name,
            state=result.state,
            turns=result.turns,
            output=(result.output or "")[:120],
            depth=self._ctx.depth + 1,
            parent_run_id=self._ctx.parent_run_id,
            child_run_id=child_run_id,
        )
        return result.as_dict()

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
            from prodagent.runtime.runner import drive

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

    async def _check_handoff_security(
        self,
        packet: HandoffPacket,
        child: ChildResult,
        name: str,
    ) -> ChildResult | None:
        """L7 handoff checkpoint — a registered checker can veto the result."""
        if not self._hooks:
            return None
        handoff_data = {
            "status": child.state,
            "result_data": {
                "agent": name,
                "output": (child.output or "")[: self._handoff_output_max_chars],
                "turns": child.turns,
            },
            "next_action": "complete",
        }
        try:
            blocked = await self._hooks.check_blocking(
                CheckPoint.AGENT_HANDOFF,
                handoff_data=handoff_data,
            )
        except SECURITY_VETO_EXCEPTIONS as sec_exc:
            logger.warning("AGENT_HANDOFF[%s]: rejected (%s)", packet.task_id[:8], sec_exc)
            return short_result(
                name,
                STATE_HANDOFF_REJECTED,
                f"Handoff rejected by security policy: {sec_exc}",
            )
        if blocked.blocked:
            return short_result(name, STATE_HANDOFF_REJECTED, "Handoff blocked by security policy")
        return None

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
            estimated_latency_ms=self._framework_config.orchestration.spawn_tool_timeout_ms,
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
    pipeline = SpawnPipeline(
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
