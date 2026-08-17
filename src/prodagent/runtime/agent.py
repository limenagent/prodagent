"""Agent — public agent class: declarative construction + execution entry."""

from __future__ import annotations

import inspect
import logging
import uuid
from dataclasses import replace as _dc_replace
from typing import TYPE_CHECKING, Any

from prodagent.backends.factory import resolve_checkpoint, resolve_session_store
from prodagent.cognition.context.manager import ContextManager
from prodagent.cognition.memory import MemoryProvider
from prodagent.core.config import FrameworkConfig
from prodagent.core.events import RunCompletedEvent, RunFailedEvent, RunSuspendedEvent
from prodagent.core.exceptions import (
    PlanAlreadyCompletedError,
    RunIdCollisionError,
    UnknownApprovalError,
)
from prodagent.core.state.run import CHILD_SEPARATOR
from prodagent.core.state.session import ConversationSession
from prodagent.core.types import ExecutionMode, MessageList, RunState
from prodagent.guardrail.approval import ApprovalDecision, ApprovalProvider
from prodagent.hooks.bundles.memory import MemoryHooks
from prodagent.hooks.checkpoint import CheckPoint, InjectionPoint
from prodagent.hooks.events import HookEvent
from prodagent.hooks.registry import HookRegistry
from prodagent.runtime._tool_merge import merge_tools_by_name
from prodagent.runtime.config import AgentConfig
from prodagent.runtime.coordination.accounting import SpawnAccumulator
from prodagent.runtime.coordination.parent_runtime import ParentRuntime
from prodagent.runtime.runner import collect_final_run, drive_stream

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from prodagent.cognition.context.spill import ToolResultSpillStore
    from prodagent.core.budget import HardBudget
    from prodagent.core.events import AgentEvent
    from prodagent.core.state.run import AgentRun
    from prodagent.evaluation.skills.registry import SkillRegistry
    from prodagent.llm.base import LLMClient
    from prodagent.mcp.config import MCPServerConfig
    from prodagent.ports import CheckpointStore, EventLog, SessionStore, Tool
    from prodagent.runtime.coordination.messaging.contract import MessageContract
    from prodagent.runtime.plan.dag import Plan
    from prodagent.runtime.run_context import RunContext
    from prodagent.runtime.workflow import Workflow
    from prodagent.tooling.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _make_injector(f: Callable[..., Any]) -> Callable[..., Any]:
    sig = inspect.signature(f)
    if len(sig.parameters) == 1:

        def _injector(**kw: Any) -> Any:
            return f(kw.get("query", ""))
    else:

        def _injector(**kw: Any) -> Any:
            return f(kw)

    return _injector


class Agent:
    """Declarative agent with a single, flat constructor."""

    def __init__(
        self,
        name: str,
        *,
        # Identity
        system_prompt: str = "",
        description: str = "",
        # Capabilities
        tools: list[Tool] | None = None,
        tool_registry: ToolRegistry | None = None,
        skills: SkillRegistry | None = None,
        # LLM
        llm: LLMClient | None = None,
        # Mode
        mode: ExecutionMode = ExecutionMode.PLAN_FIRST,
        workflow: Workflow | None = None,
        allow_replan: bool = True,
        # Limits
        budget: HardBudget | None = None,
        constraints: list[str] | None = None,
        max_replans: int = 2,
        # Topology: agents= delegates-and-returns, peers= hands-off-and-terminates.
        # See the class docstring for the full agents= vs peers= distinction.
        agents: list[Agent] | None = None,
        peers: list[Agent] | None = None,
        # Infrastructure
        framework: FrameworkConfig | None = None,
        hooks: HookRegistry | None = None,
        mcp: list[MCPServerConfig] | None = None,
        checkpoint: CheckpointStore | None = None,
        event_log: EventLog | None = None,
        session_store: SessionStore | None = None,
        spill_store: ToolResultSpillStore | None = None,
        output_contract: MessageContract | None = None,
        approval: ApprovalProvider | None = None,
        memory: MemoryProvider | None = None,
        # Extensions & hook points
        extensions: list[object] | None = None,
        injectors: list[tuple[Any, Callable[..., Any]]] | None = None,
        checkers: list[tuple[Any, Callable[..., Any]]] | None = None,
        event_handlers: list[tuple[Any, Callable[..., Any]]] | None = None,
        # Internal
        initial_plan: Plan | None = None,
        spawn_accumulator: SpawnAccumulator | None = None,
    ) -> None:
        # Build AgentConfig, filtering None values to preserve dataclass defaults
        cfg_kwargs: dict[str, Any] = {"name": name}
        for key, val in (
            ("llm", llm),
            ("system_prompt", system_prompt),
            ("description", description),
            ("tool_registry", tool_registry),
            ("skills", skills),
            ("mode", mode),
            ("budget", budget),
            ("max_replans", max_replans),
            ("framework", framework),
            ("hooks", hooks),
            ("checkpoint", checkpoint),
            ("event_log", event_log),
            ("session_store", session_store),
            ("spill_store", spill_store),
            ("output_contract", output_contract),
            ("approval", approval),
            ("memory", memory),
            ("initial_plan", initial_plan),
            ("spawn_accumulator", spawn_accumulator),
        ):
            if val is not None:
                cfg_kwargs[key] = val
        if tools is not None:
            cfg_kwargs["tools"] = list(tools)
        if constraints is not None:
            cfg_kwargs["constraints"] = list(constraints)
        if agents is not None:
            cfg_kwargs["agents"] = list(agents)
        if peers is not None:
            cfg_kwargs["peers"] = list(peers)
        if mcp is not None:
            cfg_kwargs["mcp"] = list(mcp)
        if extensions is not None:
            cfg_kwargs["extensions"] = list(extensions)
        if injectors is not None:
            cfg_kwargs["injectors"] = list(injectors)
        if checkers is not None:
            cfg_kwargs["checkers"] = list(checkers)
        if event_handlers is not None:
            cfg_kwargs["event_handlers"] = list(event_handlers)

        self.config: AgentConfig = AgentConfig(**cfg_kwargs)

        if CHILD_SEPARATOR in self.config.name:
            raise ValueError(
                f"Agent name {self.config.name!r} contains {CHILD_SEPARATOR!r} — "
                "reserved for parent::child run_id derivation; pick a name without it"
            )

        if (
            self.config.initial_plan is not None
            and self.config.mode is not ExecutionMode.PLAN_FIRST
        ):
            raise ValueError(
                "initial_plan requires PLAN_FIRST mode — pass workflow=wf to set both atomically. "
                f"Got mode={self.config.mode.value}, initial_plan is set."
            )

        self._hooks_wired: bool = False
        self._session_store: SessionStore | None = None

        # Resolve workflow eagerly
        if workflow is not None:
            from prodagent.runtime.workflow import Workflow as _Workflow

            if not isinstance(workflow, _Workflow):
                raise TypeError(f"workflow= expects a Workflow, got {type(workflow).__name__}")
            resolved_llm = self.config.llm
            if resolved_llm is None:
                from prodagent.backends.factory import resolve_llm

                resolved_llm = resolve_llm(self.framework_config)
            workflow.bind(resolved_llm, self.config.hooks)
            self.config.mode = ExecutionMode.PLAN_FIRST
            self.config.initial_plan = workflow.compile()
            if not allow_replan:
                self.config.max_replans = 0
            self.config.tools = [*self.config.tools, *workflow.tools]

    # -- Execution --------------------------------------------------------

    async def chat_stream(
        self,
        message: str = "",
        *,
        session_id: str | None = None,
        resume: bool = False,
        mode: ExecutionMode | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        if resume and not session_id:
            raise ValueError("resume=True requires an explicit session_id")

        sid = session_id or str(uuid.uuid4())
        store = self._ensure_session_store_resolved()
        if resume:
            session, run_id, resolved_mode = await self._load_suspended_turn(sid, store)
            messages: MessageList | None = None
        else:
            session, run_id, resolved_mode, messages = await self._begin_chat_turn(
                message, sid, mode
            )

        async for event in drive_stream(
            self,
            message,
            run_id=run_id,
            forced_mode=resolved_mode,
            initial_messages=messages,
        ):
            if isinstance(event, (RunCompletedEvent, RunFailedEvent, RunSuspendedEvent)):
                session.complete_turn(run_id, resolved_mode, event.run)
                await store.save(session, expected_version=session.version)
            yield event

    async def chat(
        self,
        message: str | None = None,
        *,
        session_id: str | None = None,
        resume: bool = False,
        mode: ExecutionMode | None = None,
    ) -> AgentRun:
        if message is None and not resume:
            raise ValueError(
                "chat() requires a message (or resume=True with an explicit session_id). "
                "For an interactive prompt loop, use prodagent.repl.repl_loop(agent) or the "
                "`prodagent` CLI instead."
            )
        stream = self.chat_stream(message or "", session_id=session_id, resume=resume, mode=mode)
        return await collect_final_run(
            stream,
            fallback_run_id=session_id or str(uuid.uuid4()),
            fallback_task=message or "",
        )

    async def submit_approval(
        self,
        request_id: str,
        decision: str,
        *,
        approver_id: str = "",
    ) -> None:
        gate = self._find_approval_gate()
        if gate is None:
            raise UnknownApprovalError(
                "no ApprovalGate is wired to this agent — cannot submit approval",
                request_id=request_id,
            )
        await gate.submit_decision(request_id, ApprovalDecision(decision), approver_id=approver_id)

    def _find_approval_gate(self) -> Any:
        for ext in self.config.extensions:
            if hasattr(ext, "approval_gate"):
                return ext.approval_gate
        if isinstance(self.config.approval, ApprovalProvider):
            return self.config.approval
        return None

    async def _load_suspended_turn(
        self,
        session_id: str,
        store: SessionStore,
    ) -> tuple[ConversationSession, str, ExecutionMode]:
        session = await store.load(session_id)
        if session is None:
            raise PlanAlreadyCompletedError(f"<unknown:{session_id}>")
        if session.last_turn is None or session.last_turn.state not in (
            RunState.SUSPENDED,
            RunState.RUNNING,
        ):
            raise PlanAlreadyCompletedError(
                session.last_turn.run_id if session.last_turn else f"<{session_id}>"
            )
        return session, session.last_turn.run_id, session.last_turn.mode

    async def _begin_chat_turn(
        self,
        message: str,
        session_id: str,
        mode: ExecutionMode | None,
    ) -> tuple[ConversationSession, str, ExecutionMode, MessageList]:
        resolved_mode = mode or self.config.mode
        store = self._ensure_session_store_resolved()
        session = await store.load(session_id)
        if session is None:
            session = ConversationSession(session_id=session_id, agent_id=self.config.name)
        if (
            self.config.initial_plan is not None
            and session.last_turn is not None
            and session.last_turn.state is not RunState.SUSPENDED
            and resolved_mode is ExecutionMode.PLAN_FIRST
        ):
            raise PlanAlreadyCompletedError(session.last_turn.run_id)
        alloc = session.start_turn(message, mode=resolved_mode)

        if alloc.is_new:
            checkpoint = self._ensure_checkpoint_resolved()
            if checkpoint is not None:
                orphan = await checkpoint.load(alloc.run_id)
                if orphan is not None:
                    raise RunIdCollisionError(alloc.run_id)
            await store.save(session, expected_version=session.version)

        return session, alloc.run_id, alloc.mode, alloc.messages

    def _ensure_checkpoint_resolved(self) -> Any:
        if self.config.checkpoint is None:
            self.config.checkpoint = resolve_checkpoint(self.framework_config)
        return self.config.checkpoint

    def _ensure_session_store_resolved(self) -> SessionStore:
        if self._session_store is None:
            self._session_store = resolve_session_store(self.framework_config)
        return self._session_store

    # -- Prompt & context -------------------------------------------------

    def build_system_prompt(self) -> str:
        parts: list[str] = []
        if self.config.name:
            parts.append(f"# {self.config.name} Agent")
        if self.config.system_prompt:
            parts.append(f"## Context\n{self.config.system_prompt}")
        if self.config.constraints:
            lines = "\n".join(f"- {c}" for c in self.config.constraints)
            parts.append(f"## Hard Constraints\n{lines}")
        if self.config.skills:
            section = self.config.skills.system_prompt_section()
            if section:
                parts.append(section)
        return "\n\n".join(parts)

    def build_context_manager(
        self,
        system: str,
        fw: Any,
        ctx: RunContext,
    ) -> ContextManager:
        constraints_reminder = (
            "\n".join(f"- {c}" for c in self.config.constraints) if self.config.constraints else ""
        )
        ctx_cfg = fw.context
        summary_model = ctx_cfg.summary_model or fw.summary_model
        if summary_model != ctx_cfg.summary_model:
            ctx_cfg = _dc_replace(ctx_cfg, summary_model=summary_model)
        return ContextManager(
            config=ctx_cfg,
            system_prompt=system,
            constraint_reminder=constraints_reminder,
            llm=ctx.llm,
            spill_store=ctx.spill_store or self.config.spill_store,
        )

    # -- Tools ------------------------------------------------------------

    async def resolve_tools(self) -> list[Tool]:
        active_tools: list[Tool] = list(self.config.tools)
        if self.config.tool_registry is not None:
            registry_tools = await self.config.tool_registry.get_active_tools(
                role="general", intent=""
            )
            merge_tools_by_name(active_tools, registry_tools)
        return active_tools

    # -- Hooks ------------------------------------------------------------

    def attach_default_hooks(self) -> HookRegistry | None:
        if self.config.hooks is not None:
            self._wire_hooks(self.config.hooks)
            return self.config.hooks

        registry = HookRegistry()
        from prodagent.hooks.bundles.base import default_hook_bundles

        for bundle in default_hook_bundles():
            try:
                bundle.attach(self, self.framework_config, registry)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not attach %s: %s", type(bundle).__name__, exc)

        self._wire_hooks(registry)
        self.config.hooks = registry
        return registry

    def _wire_hooks(self, hooks: HookRegistry) -> None:
        """Register accumulated injectors, checkers, event handlers, and extensions."""
        if self._hooks_wired:
            return
        self._hooks_wired = True

        for point, fn in self.config.injectors:
            if not isinstance(point, InjectionPoint):
                raise TypeError(
                    f"injector point must be an InjectionPoint member, "
                    f"got {type(point).__name__}: {point!r}"
                )
            hooks.register_injector(point, _make_injector(fn))

        for point, fn in self.config.checkers:
            if not isinstance(point, CheckPoint):
                raise TypeError(
                    f"checker point must be a CheckPoint member, "
                    f"got {type(point).__name__}: {point!r}"
                )
            hooks.register_checker(point, fn)

        for event_name, fn in self.config.event_handlers:
            if not isinstance(event_name, HookEvent):
                raise TypeError(
                    f"event handler event must be a HookEvent member, "
                    f"got {type(event_name).__name__}: {event_name!r}"
                )
            hooks.register_event(event_name, fn)

        for ext in self.config.extensions:
            hooks.attach_extension(ext)

    # -- Fork / spawn -----------------------------------------------------

    def peer_named(self, name: str) -> Agent | None:
        for peer in self.config.peers:
            if peer.name == name:
                return peer
        return None

    def _build_fork_skeleton(self, runtime: ParentRuntime) -> Agent:
        return Agent(
            self.name,
            tools=list(self.inline_tools),
            system_prompt=self.system_prompt,
            llm=runtime.llm,
            hooks=runtime.hooks,
            framework=runtime.framework_config,
            constraints=list(runtime.constraints),
            budget=runtime.budget,
            mode=self.mode,
            checkpoint=runtime.checkpoint,
            event_log=runtime.event_log,
            spawn_accumulator=runtime.accumulator,
        )

    def fork_as_peer(
        self,
        parent: Agent,
        parent_run_id: str | None,
        *,
        checkpoint: CheckpointStore | None = None,
        event_log: EventLog | None = None,
    ) -> Agent:
        runtime = ParentRuntime(
            llm=self.config.llm,
            hooks=parent.hooks,
            framework_config=parent.framework_config,
            constraints=parent.constraints,
            budget=self.budget_config,
            checkpoint=checkpoint if checkpoint is not None else parent.config.checkpoint,
            event_log=event_log if event_log is not None else parent.config.event_log,
            accumulator=self.config.spawn_accumulator or SpawnAccumulator(),
        )
        forked = self._build_fork_skeleton(runtime)
        forked.config.extensions = list(parent.config.extensions)
        forked.config.injectors = list(parent.config.injectors)
        forked.config.checkers = list(parent.config.checkers)
        forked.config.event_handlers = list(parent.config.event_handlers)
        forked.config.mcp = list(parent.config.mcp)
        forked._hooks_wired = parent._hooks_wired
        forked.config.peers = list(self.config.peers)
        forked.config.description = self.config.description
        return forked

    def fork_as_spawn(self, runtime: ParentRuntime) -> Agent:
        forked = self._build_fork_skeleton(runtime)
        forked.config.extensions = list(self.config.extensions)
        forked.config.injectors = list(self.config.injectors)
        forked.config.checkers = list(self.config.checkers)
        forked.config.event_handlers = list(self.config.event_handlers)
        forked.config.mcp = list(self.config.mcp)
        forked._hooks_wired = self._hooks_wired
        if self.config.initial_plan is not None:
            forked.config.initial_plan = self.config.initial_plan
            forked.config.max_replans = self.config.max_replans
        if self.config.description:
            forked.config.description = self.config.description
        return forked

    # -- Properties -------------------------------------------------------

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def system_prompt(self) -> str:
        return self.config.system_prompt

    @property
    def mode(self) -> ExecutionMode:
        return self.config.mode

    @property
    def initial_plan(self) -> Any:
        return self.config.initial_plan

    @property
    def max_replans(self) -> int:
        return self.config.max_replans

    @property
    def skills(self) -> Any:
        return self.config.skills

    @property
    def checkpoint(self) -> Any:
        return self._ensure_checkpoint_resolved()

    @property
    def mcp_configs(self) -> list[Any]:
        return list(self.config.mcp)

    @property
    def memory_manager(self) -> Any:
        for ext in self.config.extensions:
            if isinstance(ext, MemoryHooks):
                return ext.memory_manager
        if isinstance(self.config.memory, MemoryProvider):
            return self.config.memory
        return None

    @property
    def framework_config(self) -> FrameworkConfig:
        if self.config.framework is None:
            self.config.framework = FrameworkConfig.from_env()
        return self.config.framework

    @property
    def inline_tools(self) -> list[Tool]:
        return list(self.config.tools)

    @inline_tools.setter
    def inline_tools(self, tools: list[Tool]) -> None:
        self.config.tools = list(tools)

    @property
    def budget_config(self) -> Any:
        return self.config.budget

    @property
    def child_agents(self) -> list[Agent]:
        return list(self.config.agents)

    @property
    def hooks(self) -> HookRegistry | None:
        return self.config.hooks

    @property
    def constraints(self) -> list[str]:
        return list(self.config.constraints)

    @property
    def tool_registry(self) -> Any:
        return self.config.tool_registry

    @property
    def description(self) -> str:
        return self.config.description

    @property
    def injectors(self) -> list[tuple[Any, Any]]:
        return list(self.config.injectors)

    @property
    def checkers(self) -> list[tuple[Any, Any]]:
        return list(self.config.checkers)

    @property
    def event_handlers(self) -> list[tuple[Any, Any]]:
        return list(self.config.event_handlers)
