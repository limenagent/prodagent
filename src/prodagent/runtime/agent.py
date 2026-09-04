"""Agent — public agent class: declarative construction + execution entry."""

from __future__ import annotations

import dataclasses
import inspect
import logging
import uuid
from typing import TYPE_CHECKING, Any, cast

from prodagent.base.config import FrameworkConfig
from prodagent.base.errors import (
    UnknownApprovalError,
)
from prodagent.kernel.budget import SpawnAccumulator
from prodagent.kernel.bus import Gate, HookEvent, HookRegistry, InjectionPoint
from prodagent.kernel.run import CHILD_SEPARATOR, collect_final_run
from prodagent.kernel.types import (
    MessageList,
    RunCompletedEvent,
    RunFailedEvent,
    RunSuspendedEvent,
)
from prodagent.ports.execution import AgentSpec
from prodagent.ports.llm import LLMClient
from prodagent.runtime.config import AgentConfig
from prodagent.runtime.runner import drive_stream
from prodagent.tooling.merge import merge_tools_by_name

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Sequence

    from prodagent.cognition.context.manager import ContextManager
    from prodagent.cognition.memory import MemoryProvider
    from prodagent.kernel.budget import HardBudget
    from prodagent.kernel.run import Run
    from prodagent.kernel.types import AgentEvent
    from prodagent.mcp.config import MCPServerConfig
    from prodagent.ports import CheckpointStore, EventLog, SessionStore, Tool
    from prodagent.runtime.runner import RunContext
    from prodagent.skills.registry import SkillRegistry

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


_CONFIG_FIELD_NAMES = frozenset(f.name for f in dataclasses.fields(AgentConfig))


class Agent:
    """Declarative agent — hot params for the common path, AgentConfig for the rest."""

    def __init__(
        self,
        name: str = "",
        *,
        system_prompt: str | None = None,
        tools: Sequence[Tool] | None = None,
        budget: HardBudget | None = None,
        config: AgentConfig | None = None,
    ) -> None:
        if config is not None:
            if name and name != config.name:
                raise ValueError(
                    f"Agent(name={name!r}) conflicts with config.name={config.name!r} — "
                    "pass the name once"
                )
            name = config.name
        if not name:
            raise ValueError("Agent requires a name — positional or via config.name")
        cfg = config if config is not None else AgentConfig(name=name)
        if system_prompt is not None:
            cfg.system_prompt = system_prompt
        if tools is not None:
            cfg.tools = list(tools)  # defensive copy — never alias caller lists
        if budget is not None:
            cfg.budget = budget
        if cfg.llm is not None and not isinstance(cfg.llm, LLMClient):
            raise TypeError(
                f"AgentConfig.llm expects an LLMClient instance (e.g. OpenAIAdapter, "
                f"AnthropicAdapter) — got {type(cfg.llm).__name__}. "
                "If you built an LLMConfig, pass it as the adapter's default_config= "
                "instead: OpenAIAdapter(..., default_config=your_llm_config)."
            )
        self.config: AgentConfig = cfg
        self._bind_invariants()

    @classmethod
    def _from_config(cls, config: AgentConfig) -> Agent:
        """Private alternate constructor for forks: assemble the Agent around an
        already-built config instead of re-threading 30 keyword arguments."""
        self = cls.__new__(cls)
        self.config = config
        self._bind_invariants()
        return self

    def _bind_invariants(self) -> None:
        """Constructor invariants, shared by both construction paths."""
        if CHILD_SEPARATOR in self.config.name:
            raise ValueError(
                f"Agent name {self.config.name!r} contains {CHILD_SEPARATOR!r} — "
                "reserved for parent::child run_id derivation; pick a name without it"
            )

        self._hooks_wired: bool = False
        self._session_store: SessionStore | None = None
        self._plan_event_log: EventLog | None = None
        self._plan_checkpoint_store: CheckpointStore | None = None

    # -- Execution --------------------------------------------------------

    async def chat_stream(
        self,
        message: str = "",
        *,
        session_id: str | None = None,
        resume: bool = False,
        as_unit: bool = False,
    ) -> AsyncGenerator[AgentEvent, None]:
        """One conversational turn as an event stream. Session-scoped: the
        session allocates (or resumes) the run identity, the transcript folds
        back into the session when a terminal event lands, and the session
        persists under optimistic versioning — two concurrent turns on one
        session conflict loudly instead of last-write-wins."""
        if resume and not session_id:
            raise ValueError("resume=True requires an explicit session_id")

        from prodagent.runtime.runner import begin_chat_turn, load_suspended_turn, tape_prefixed

        sid = session_id or str(uuid.uuid4())
        store = self._ensure_session_store_resolved()
        if resume:
            session, run_id, single_unit = await load_suspended_turn(self, sid, store)
            messages: MessageList | None = None
        else:
            session, run_id, single_unit, messages = await begin_chat_turn(
                self, message, sid, as_unit=as_unit
            )

        # A consumer that abandons this stream mid-run leaves the turn RUNNING
        # on disk — deliberately the same durable state a hard crash produces,
        # because an abandoned run can be mid-tool-call and is NOT cleanly
        # suspendable (a clean suspend goes through RunSuspendedEvent at a
        # step boundary). Both load paths resume such a turn identically; see
        # _load_suspended_turn.
        async for event in drive_stream(
            self,
            message,
            run_id=tape_prefixed(run_id),
            single_unit=single_unit,
            initial_messages=messages,
        ):
            if isinstance(event, (RunCompletedEvent, RunFailedEvent, RunSuspendedEvent)):
                # Fold the finished transcript back and persist before the
                # consumer sees the terminal event — a crash right after
                # still leaves a resumable session on disk.
                session.complete_turn(run_id, single_unit, event.run)
                await store.save(session, expected_version=session.version)
            yield event

    async def chat(
        self,
        message: str | None = None,
        *,
        session_id: str | None = None,
        resume: bool = False,
        as_unit: bool = False,
    ) -> Run:
        """Stream-and-settle convenience over ``chat_stream`` — reduces the
        stream to its terminal run (a synthetic FAILED one if the stream
        somehow ends bare)."""
        if message is None and not resume:
            raise ValueError(
                "chat() requires a message (or resume=True with an explicit session_id). "
                "For the visual playground, run the `prodagent` CLI instead."
            )
        stream = self.chat_stream(
            message or "", session_id=session_id, resume=resume, as_unit=as_unit
        )
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
        """Answer an approval request this agent's run is parked on. The
        decision lands in the store; resumption happens by re-driving the
        session (``resume=True``) — no in-process waiter is woken, which is
        what lets the deciding human be on another machine."""
        from prodagent.hooks.approval import ApprovalDecision
        from prodagent.runtime.runner import find_approval_gate

        gate = find_approval_gate(self)
        if gate is None:
            raise UnknownApprovalError(
                "no ApprovalGate is wired to this agent — cannot submit approval",
                request_id=request_id,
            )
        await gate.submit_decision(request_id, ApprovalDecision(decision), approver_id=approver_id)

    def _ensure_checkpoint_resolved(self) -> CheckpointStore | None:
        from prodagent.backends.factory import resolve_checkpoint

        self.config.checkpoint = resolve_checkpoint(self.framework_config, self.config.checkpoint)
        return self.config.checkpoint

    def _ensure_session_store_resolved(self) -> SessionStore:
        from prodagent.backends.factory import in_memory_session_store, resolve_session_store

        self._session_store = resolve_session_store(self.framework_config, self._session_store)
        if self._session_store is None:
            # A session cannot function without a store: bare gets the
            # in-process one (state dies with the process — the bare contract).
            self._session_store = in_memory_session_store()
        return self._session_store

    def ensure_plan_event_log_fallback(self) -> EventLog:
        """Graph tracking always needs a working event log — unlike the
        single-unit shape (where ``ctx.event_log`` ``None`` is valid), a
        bare profile still gets a real (in-process) store here, cached on
        the agent so repeated hops/resumes within the same process share it."""
        if self._plan_event_log is None:
            from prodagent.backends.factory import in_memory_event_log

            self._plan_event_log = in_memory_event_log()
        return self._plan_event_log

    def ensure_plan_checkpoint_fallback(self) -> CheckpointStore:
        """Counterpart to :meth:`ensure_plan_event_log_fallback` for the
        checkpoint side of graph tracking's persistence pair."""
        if self._plan_checkpoint_store is None:
            from prodagent.backends.factory import in_memory_checkpoint_store

            self._plan_checkpoint_store = in_memory_checkpoint_store()
        return self._plan_checkpoint_store

    # -- Prompt & context -------------------------------------------------

    def build_system_prompt(self) -> str:
        """Compose the per-agent system prompt: identity header, context,
        hard constraints, skills index. The ``# {name} Agent`` header is
        load-bearing — RoutingFakeLLM routes test scripts on it."""
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
        from prodagent.cognition.context.manager import ContextManager

        constraints_reminder = (
            "\n".join(f"- {c}" for c in self.config.constraints) if self.config.constraints else ""
        )
        from prodagent.backends.factory import resolve_aux_llm

        ctx_cfg = fw.context
        return ContextManager(
            config=ctx_cfg,
            system_prompt=system,
            constraint_reminder=constraints_reminder,
            llm=ctx.llm,
            spill_store=ctx.spill_store or self.config.spill_store,
            aux_llm=resolve_aux_llm(fw),
        )

    # -- Tools ------------------------------------------------------------

    async def resolve_tools(self) -> list[Tool]:
        """This agent's base tool set: inline tools first, then registry
        visibility (L1/L2/L3 + breaker filtering) merged by name — inline
        wins by merge order, the "closest to the developer" rule."""
        active_tools: list[Tool] = list(self.config.tools)
        if self.config.tool_registry is not None:
            registry_tools = await self.config.tool_registry.get_active_tools(
                role="general", intent=""
            )
            merge_tools_by_name(active_tools, registry_tools)
        return active_tools

    # -- Hooks ------------------------------------------------------------

    def attach_default_hooks(self) -> HookRegistry | None:
        """Idempotent hook wiring: an explicitly passed registry is only
        user-wired onto it; otherwise the profile's bundle manifest mounts
        (each cartridge attaches itself), then user accumulations wire on
        top. A bundle that fails to attach logs and steps aside — one bad
        cartridge must not take the agent down."""
        if self.config.hooks is not None:
            self._wire_hooks(self.config.hooks)
            return self.config.hooks

        registry = HookRegistry()
        from prodagent.hooks.bundles.base import default_hook_bundles

        for bundle in default_hook_bundles(self.framework_config):
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

        for inject_point, fn in self.config.injectors:
            if not isinstance(inject_point, InjectionPoint):
                raise TypeError(
                    f"injector point must be an InjectionPoint member, "
                    f"got {type(inject_point).__name__}: {inject_point!r}"
                )
            hooks.register_injector(inject_point, _make_injector(fn))

        for gate_point, fn in self.config.checkers:
            if not isinstance(gate_point, Gate):
                raise TypeError(
                    f"checker point must be a Gate member, "
                    f"got {type(gate_point).__name__}: {gate_point!r}"
                )
            hooks.register_checker(gate_point, fn)

        for event, fn in self.config.event_handlers:
            if not isinstance(event, HookEvent):
                raise TypeError(
                    f"event handler event must be a HookEvent member, "
                    f"got {type(event).__name__}: {event!r}"
                )
            hooks.register_event(event, fn)

        for ext in self.config.extensions:
            hooks.attach_extension(ext)

    # -- Wire projection ---------------------------------------------------

    def spec(self) -> AgentSpec:
        """Serializable projection of this agent — the wire form for remote
        spawn and rosters (:class:`prodagent.ports.execution.AgentSpec`).
        Live wiring (LLM client, hooks, stores, tool callables) stays on
        AgentConfig and never crosses a process boundary."""
        return AgentSpec(
            name=self.config.name,
            description=self.config.description,
            system_prompt=self.config.system_prompt,
            constraints=list(self.config.constraints),
            budget=self.config.budget,
            tools_schema=[t.schema for t in self.config.tools],
            child_agents=[a.spec() for a in self.config.agents],
            peers=[a.spec() for a in self.config.peers],
        )

    # -- Fork / spawn -----------------------------------------------------

    def peer_named(self, name: str) -> Agent | None:
        """Roster lookup by name — what a handoff descriptor resolves
        against on the receiving side."""
        for peer in self.config.peers:
            if peer.name == name:
                return peer
        return None

    def _fork(self, **overrides: Any) -> Agent:
        """Derive a child agent by field-replacing this agent's config. Every
        AgentConfig field propagates unless explicitly overridden, so a field
        added later can never silently drop out of spawn/peer forks (the
        hand-copied field list this replaces had exactly that failure mode)."""
        forked = Agent._from_config(dataclasses.replace(self.config, **overrides))
        forked._hooks_wired = self._hooks_wired
        return forked

    def _runtime_overrides(self, ctx: Any) -> dict[str, Any]:
        """The field-replacement set a fork takes from the hop's wiring —
        the resolved subset (budget, stores, llm, hooks), never the parent's
        per-hop state. ``ctx`` is the hop's RunContext (or a plain parent
        Agent for the peer path)."""
        if hasattr(ctx, "agent"):
            return {
                "llm": ctx.llm,
                "hooks": ctx.agent.hooks,
                "framework": ctx.agent.framework_config,
                "constraints": list(ctx.agent.constraints),
                "budget": ctx.agent.budget_config,
                "checkpoint": ctx.checkpoint,
                "event_log": ctx.event_log,
                "spawn_accumulator": self.config.spawn_accumulator or SpawnAccumulator(),
            }
        return {
            "llm": ctx.config.llm,
            "hooks": ctx.hooks,
            "framework": ctx.framework_config,
            "constraints": list(ctx.constraints),
            "budget": self.budget_config,
            "checkpoint": ctx.config.checkpoint,
            "event_log": ctx.config.event_log,
            "spawn_accumulator": self.config.spawn_accumulator or SpawnAccumulator(),
        }

    # -- Properties -------------------------------------------------------

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def skills(self) -> SkillRegistry | None:
        return self.config.skills

    @property
    def checkpoint(self) -> CheckpointStore | None:
        return self._ensure_checkpoint_resolved()

    @property
    def mcp_configs(self) -> list[MCPServerConfig]:
        return list(self.config.mcp)

    @property
    def memory_manager(self) -> MemoryProvider | None:
        # lazy: keeps the memory bundle (and the kernel import chain) out of
        # module-level imports — see tests/base/test_import_weight.py
        from prodagent.cognition.memory import MemoryProvider

        # Idempotent wire-first: what a probe sees is what a run would use.
        self.attach_default_hooks()
        hooks = self.config.hooks
        if hooks is not None:
            manager = hooks.require(MemoryProvider)
            if manager is not None:
                return cast("MemoryProvider", manager)
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
    def budget_config(self) -> HardBudget | None:
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
    def event_handlers(self) -> list[tuple[HookEvent, Callable[..., Any]]]:
        return list(self.config.event_handlers)
