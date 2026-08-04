"""Agent — public agent class: declarative fluent builder + execution entry."""

from __future__ import annotations

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
from prodagent.core.state.run import CHILD_SEPARATOR, make_failed_run
from prodagent.core.state.session import ConversationSession
from prodagent.core.types import ExecutionMode, MessageList, RunState
from prodagent.guardrail.approval import ApprovalDecision, ApprovalProvider
from prodagent.hooks.bundles.memory import MemoryHooks
from prodagent.hooks.bundles.security import ApprovalHooks
from prodagent.hooks.registry import HookRegistry
from prodagent.runtime.config import AgentConfig, merge_tools_by_name
from prodagent.runtime.fluent import AgentFluentMixin
from prodagent.runtime.runner import drive_stream
from prodagent.tooling.reliability.locks import LockRegistry

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from prodagent.core.events import AgentEvent
    from prodagent.core.state.run import AgentRun
    from prodagent.ports import SessionStore, Tool
    from prodagent.runtime.session import RunContext

logger = logging.getLogger(__name__)


class Agent(AgentFluentMixin):
    def __init__(self, name: str, **kwargs: Any) -> None:
        lock_registry = kwargs.pop("lock_registry", None)
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        self.config: AgentConfig = AgentConfig(name=name, **kwargs)

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
                "initial_plan requires PLAN_FIRST mode — .workflow() sets both atomically. "
                f"Got mode={self.config.mode.value}, initial_plan is set."
            )

        self._fluent_wired: bool = False
        self._session_store: SessionStore | None = None
        self._lock_registry = lock_registry or LockRegistry()

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
        final_run: AgentRun | None = None
        async for event in self.chat_stream(
            message or "", session_id=session_id, resume=resume, mode=mode
        ):
            if isinstance(event, (RunCompletedEvent, RunFailedEvent, RunSuspendedEvent)):
                final_run = event.run
        if final_run is None:
            return make_failed_run(session_id or str(uuid.uuid4()), message or "")
        return final_run

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
            if isinstance(ext, ApprovalHooks):
                return ext.approval_gate
        if isinstance(self.config.approval_gate, ApprovalProvider):
            return self.config.approval_gate
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

    def build_system_prompt(self) -> str:
        parts: list[str] = []
        if self.config.name:
            parts.append(f"# {self.config.name} Agent")
        if self.config.context:
            parts.append(f"## Context\n{self.config.context}")
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

    async def resolve_tools(self) -> list[Tool]:
        active_tools: list[Tool] = list(self.config.tools)
        if self.config.tool_registry is not None:
            registry_tools = await self.config.tool_registry.get_active_tools(
                role="general", intent=""
            )
            merge_tools_by_name(active_tools, registry_tools)
        return active_tools

    def attach_default_hooks(self) -> HookRegistry | None:
        if self.config.hooks is not None:
            self.wire_fluent_hooks(self.config.hooks)
            return self.config.hooks

        registry = HookRegistry()
        from prodagent.hooks.bundles.base import default_hook_bundles

        for bundle in default_hook_bundles():
            try:
                bundle.attach(self, self.framework_config, registry)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not attach %s: %s", type(bundle).__name__, exc)

        self.wire_fluent_hooks(registry)
        self.config.hooks = registry
        return registry

    def peer_named(self, name: str) -> Agent | None:
        for peer in self.config.peer_agents:
            if peer.name == name:
                return peer
        return None

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def system_prompt(self) -> str:
        return self.config.context

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
        return list(self.config.mcp_configs)

    @property
    def memory_manager(self) -> Any:
        for ext in self.config.extensions:
            if isinstance(ext, MemoryHooks):
                return ext.memory_manager
        if isinstance(self.config.memory_manager, MemoryProvider):
            return self.config.memory_manager
        return None

    @property
    def framework_config(self) -> FrameworkConfig:
        if self.config.framework_config is None:
            self.config.framework_config = FrameworkConfig.from_env()
        return self.config.framework_config

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
        return list(self.config.child_agents)

    @property
    def lock_registry(self) -> LockRegistry:
        return self._lock_registry

    @property
    def hooks(self) -> HookRegistry | None:
        return self.config.hooks

    @property
    def constraints(self) -> list[str]:
        return list(self.config.constraints)

    @property
    def tool_registry(self) -> Any:
        return self.config.tool_registry
