"""Spawn — vertical sub-agent delegation (``agents=``).

A single LLM tool call decides dispatch and the parent keeps running while it
waits for a result — there is no round loop here. Contrast with the three
stage-driven topologies (ensemble/blackboard/work_queue), which iterate
rounds over a shared store until a :class:`~prodagent.coordination.termination.TerminationPolicy`
fires. Spawn and :mod:`~prodagent.coordination.peer` are *delegation
strategies*; they are not a fourth and fifth "topology."
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from prodagent.base.errors import SECURITY_VETO_EXCEPTIONS, ErrorReason
from prodagent.coordination.messaging.contract import (
    DEFAULT_CHILD_CONTRACT,
    MessageContract,
)
from prodagent.coordination.messaging.envelope import (
    Crossing,
    CrossingKind,
    Direction,
)
from prodagent.coordination.messaging.packet import HandoffPacket
from prodagent.coordination.messaging.transport import TransportSpec, build_transport
from prodagent.kernel.budget import BudgetLedger, SpawnAccumulator, run_enveloped
from prodagent.kernel.bus import HookEvent
from prodagent.kernel.state import collect_final_run
from prodagent.kernel.types import (
    ErrorSeverity,
    RunState,
    SideEffectLevel,
    ToolError,
    ToolMeta,
)
from prodagent.ports.runner import AgentActivation, RunnerPort
from prodagent.tooling.base import FunctionTool
from prodagent.tooling.merge import attach_tools

if TYPE_CHECKING:
    from prodagent.base.config import FrameworkConfig
    from prodagent.kernel.budget import HardBudget
    from prodagent.kernel.bus import HookRegistry
    from prodagent.ports.agent_spec import AgentSpec
    from prodagent.ports.dead_letter import DeadLetterStore
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


def child_run_id_for(parent_run_id: str | None, name: str) -> str | None:
    from prodagent.kernel.state import child_run_id

    return child_run_id(parent_run_id, name) if parent_run_id else None


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


def build_spawn_tool_schema(specs: list[AgentSpec]) -> dict[str, Any]:
    """The model-facing roster, built from wire-shaped specs (Agent.spec()
    projections) — the same form a remote roster would send."""
    agent_lines = "\n".join(f"  - {s.name}: {s.describe()}" for s in specs)
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
                    "enum": [s.name for s in specs],
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
        runner: RunnerPort,
        hooks: HookRegistry | None = None,
        framework_config: FrameworkConfig | None = None,
        constraints: list[str] | None = None,
        budget: HardBudget | None = None,
        budget_ledger: BudgetLedger | None = None,
        parent_run_id: str | None = None,
        depth: int = 0,
        accumulator: SpawnAccumulator | None = None,
        dead_letter_queue: DeadLetterStore | None = None,
    ) -> None:
        from prodagent.backends.factory import resolve_dead_letter
        from prodagent.base.config import FrameworkConfig

        self._spec_map = {a.name: a for a in agents}
        self._runner = runner
        self._hooks = hooks
        self._framework_config = framework_config or FrameworkConfig.from_env()
        self._constraints = list(constraints or ())
        self._parent_run_id = parent_run_id
        self._depth = depth
        self._accumulator = accumulator if accumulator is not None else SpawnAccumulator()
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
        self._budget_ledger = budget_ledger or (
            BudgetLedger(max=budget) if budget is not None else None
        )
        # Both boundary directions of the spawn primitive, built through the
        # shared transport factory — preset selection and dedupe policy live
        # once there, not per primitive.
        self._dispatch_transport = build_transport(
            TransportSpec(
                direction=Direction.DOWNSTREAM,
                dead_letter=self.dlq,
                dedupe_ttl_s=orch.handoff_idempotency_ttl_s,
                hooks=self._hooks,
            )
        )
        self._result_transport = build_transport(
            TransportSpec(
                direction=Direction.UPSTREAM,
                dead_letter=self.dlq,
                contract=self._contract_for,
                trim=self._bound_result,
                hooks=self._hooks,
                audit_event=self._result_audit_event,
                max_chars=self._handoff_output_max_chars,
            )
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
        return self._accumulator

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

        packet = self._build_packet(name, spec, task, input_refs, idempotency_key)
        child_run_id = child_run_id_for(self._parent_run_id, name) if self._parent_run_id else None

        # DOWNSTREAM: dispatch (dedupe + gate); rejected dispatches die before
        # the child burns any budget.
        short = await self._dispatch(packet, name, child_run_id)
        if short is not None:
            return short

        await self._fire(
            HookEvent.AGENT_SPAWN,
            name=name,
            task=task,
            packet_id=packet.task_id,
            depth=self._depth + 1,
            parent_run_id=self._parent_run_id,
            child_run_id=child_run_id,
        )

        exhausted = await self._run_enveloped(name, spec, task, packet, child_run_id)
        if isinstance(exhausted, dict):
            return exhausted
        result = exhausted

        if result.state == STATE_TIMEOUT:
            return ToolError.from_reason(
                ErrorReason.TIMEOUT,
                code="subagent_timeout",
                message=f"Sub-agent {name!r} timed out after {result.output}",
                hint="The child ran past its wall-clock budget. Do not retry the same spawn — investigate the child's plan or raise its budget.",
                severity=ErrorSeverity.RED,
            ).as_dict()

        if result.state == STATE_SUSPENDED and result.approval_request_id:
            self._accumulator.add(result)
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

        self._accumulator.add(result)
        return await self._admit(name, result, packet, child_run_id)

    def _build_packet(
        self,
        name: str,
        spec: Agent,
        task: str,
        input_refs: dict[str, str] | None,
        idempotency_key: str,
    ) -> HandoffPacket:
        packet_kwargs: dict[str, Any] = {
            "task_description": task,
            "constraints": list(self._constraints),
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
        return packet

    async def _dispatch(
        self, packet: HandoffPacket, name: str, child_run_id: str | None
    ) -> dict[str, Any] | None:
        """Dispatch the packet DOWNSTREAM; a dict means the spawn ends here."""
        dispatch = await self._dispatch_transport.send(
            Crossing.mint(
                direction=Direction.DOWNSTREAM,
                kind=CrossingKind.DISPATCH,
                from_agent="parent",
                to=name,
                payload=packet,
                trace_id=self._parent_run_id or "",
                message_id=packet.message_id,
                depth=self._depth + 1,
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
        return None

    async def _run_enveloped(
        self,
        name: str,
        spec: Agent,
        task: str,
        packet: HandoffPacket,
        child_run_id: str | None,
    ) -> ChildResult | dict[str, Any]:
        """Run the child inside the shared settlement envelope
        (:func:`prodagent.kernel.budget.run_enveloped`) — the same
        reserve → act → commit policy the stage drivers use. A dict back means
        the ledger rejected the reservation before the child started."""
        box: list[ChildResult] = []

        async def _act() -> tuple[int, int, float]:
            result = await self._run_with_timeout(spec, task, packet, child_run_id)
            box.append(result)
            return (result.turns, result.input_tokens + result.output_tokens, result.cost_usd)

        settled = await run_enveloped(self._budget_ledger, member=name, act=_act)
        if settled is None:
            return ToolError.from_reason(
                ErrorReason.BUDGET_EXCEEDED,
                code="spawn_budget_exhausted",
                message=f"Cannot spawn {name!r}: shared budget ceiling reached for this chain.",
                hint="Concurrent sub-agent spend has hit the shared budget ceiling.",
                severity=ErrorSeverity.RED,
            ).as_dict()
        return box[0]

    async def _admit(
        self,
        name: str,
        result: ChildResult,
        packet: HandoffPacket,
        child_run_id: str | None,
    ) -> dict[str, Any]:
        """UPSTREAM: the child's result enters the parent's world through the
        admission pipeline (per-spec contract → trim → security gate → audit).
        Same message_id as the dispatch — one logical crossing, two
        directions. Rejections are dead-lettered at the pipeline boundary;
        strict contract violations never reach the parent."""
        delivery = await self._result_transport.send(
            Crossing.mint(
                direction=Direction.UPSTREAM,
                kind=CrossingKind.RESULT,
                from_agent=name,
                to="parent",
                payload=result.as_dict(),
                trace_id=self._parent_run_id or "",
                message_id=packet.message_id,
                depth=self._depth + 1,
                parent_run_id=self._parent_run_id,
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
        child_task = packet.to_task_prompt()

        try:
            run = await collect_final_run(
                self._runner.activate(
                    AgentActivation(
                        agent=spec,
                        task=child_task,
                        run_id=child_run_id,
                        parent_run_id=self._parent_run_id,
                        depth=self._depth + 1,
                        budget_ledger=self._budget_ledger,
                    )
                ),
                fallback_run_id=child_run_id or spec.name,
                fallback_task=child_task,
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
        schema = build_spawn_tool_schema([a.spec() for a in self._spec_map.values()])
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
    runner: RunnerPort,
    hooks: HookRegistry | None = None,
    framework_config: FrameworkConfig | None = None,
    constraints: list[str] | None = None,
    budget: HardBudget | None = None,
    budget_ledger: BudgetLedger | None = None,
    parent_run_id: str | None = None,
    depth: int = 0,
    accumulator: SpawnAccumulator | None = None,
    dead_letter_queue: DeadLetterStore | None = None,
) -> SpawnTool | None:
    if not agents:
        return None

    pipeline = Spawn(
        agents,
        runner=runner,
        hooks=hooks,
        framework_config=framework_config,
        constraints=constraints,
        budget=budget,
        budget_ledger=budget_ledger,
        parent_run_id=parent_run_id,
        depth=depth,
        accumulator=accumulator,
        dead_letter_queue=dead_letter_queue,
    )
    return SpawnTool(tool=pipeline.build_tool(), accumulator=pipeline.accumulator)


def assemble_spawn_tools(
    ctx: Any,
    active_tools: list[Any],
    tool_schemas: list[dict[str, Any]],
) -> SpawnAccumulator | None:
    """Build spawn tools for ``agent.child_agents`` and append them to
    ``active_tools``/``tool_schemas``.

    ``ctx`` is the hop's RunContext — runtime vocabulary, read structurally
    (agent, runner, budget ledger, identity). Coordination never imports the
    runtime; child execution reaches it only through ``ctx.runner``."""
    agent = ctx.agent
    if not agent.child_agents:
        return None
    if ctx.runner is None:
        raise RuntimeError(
            "spawn tools need the hop's RunnerPort — RunLoop wires ctx.runner "
            "before executor preparation"
        )
    spawn_tools = build_spawn_tools_for_agent(
        agent.child_agents,
        runner=ctx.runner,
        hooks=agent.hooks,
        framework_config=agent.framework_config,
        constraints=agent.constraints,
        budget=agent.budget_config,
        budget_ledger=ctx.budget_ledger,
        parent_run_id=ctx.run_id,
        depth=ctx.depth,
    )
    if spawn_tools is None:
        return None
    attach_tools(active_tools, tool_schemas, [spawn_tools.tool])
    return spawn_tools.accumulator
