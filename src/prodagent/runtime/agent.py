"""Agent — public agent class: declarative construction + execution entry."""

from __future__ import annotations

import dataclasses
import inspect
import logging
import uuid
from typing import TYPE_CHECKING, Any, cast

from prodagent.base.config import FrameworkConfig
from prodagent.base.errors import (
    PlanAlreadyCompletedError,
    RunIdCollisionError,
    UnknownApprovalError,
)
from prodagent.base.session import ConversationSession
from prodagent.kernel.budget import SpawnAccumulator
from prodagent.kernel.bus import Gate, HookEvent, HookRegistry, InjectionPoint
from prodagent.kernel.state import CHILD_SEPARATOR, collect_final_run
from prodagent.kernel.types import (
    ExecutionMode,
    MessageList,
    RunCompletedEvent,
    RunFailedEvent,
    RunState,
    RunSuspendedEvent,
)
from prodagent.ports.execution import AgentSpec
from prodagent.ports.llm import LLMClient
from prodagent.runtime.config import AgentConfig
from prodagent.runtime.parent_runtime import ParentRuntime
from prodagent.runtime.runner import drive_stream
from prodagent.tooling.merge import merge_tools_by_name

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Sequence

    from prodagent.cognition.context.manager import ContextManager
    from prodagent.cognition.memory import MemoryProvider
    from prodagent.hooks.approval import ApprovalProvider
    from prodagent.kernel.budget import HardBudget
    from prodagent.kernel.state import AgentRun
    from prodagent.kernel.types import AgentEvent
    from prodagent.mcp.config import MCPServerConfig
    from prodagent.plan.workflow import Workflow
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
    """Declarative agent — hot params for the common path, AgentConfig for the rest.

    The constructor surface is deliberately two-tier: the handful of
    parameters almost every agent sets (``system_prompt`` / ``tools`` /
    ``mode`` / ``budget`` / ``workflow``) stay keyword-friendly, while
    everything else — LLM client, topology, storage, hooks, extensions —
    is a field on :class:`AgentConfig` (``runtime/config.py``). Hot params
    override the matching config fields when both are given.

    Construction sequence — "where do tools/hooks come from" spans three
    files and two distinct times (constructor time vs. per-hop time); this
    is the map so no one has to reconstruct it by stepping through a
    debugger:

    1. **``Agent.__init__`` (eager, once)** — merges hot params into
       ``AgentConfig``, then ``_bind_invariants`` resolves ``workflow=`` if
       given: it binds the workflow to an LLM, compiles it into
       ``config.initial_plan``, and appends ``workflow.tools`` to
       ``config.tools``. Everything else on ``AgentConfig`` stays exactly
       as passed — no other resolution happens here.
    2. **``Agent.attach_default_hooks`` (lazy, idempotent, first call wins)**
       — called by both probes (``_find_approval_gate``, ``memory_manager``)
       and the real run path. If ``config.hooks`` is already set, wires it;
       otherwise builds a fresh ``HookRegistry``, attaches
       ``default_hook_bundles(framework_config)`` (``hooks/bundles/base.py``),
       then calls ``_wire_hooks`` to register accumulated injectors /
       checkers / event handlers / extensions from ``AgentConfig``.
       ``_hooks_wired`` guards against double-registration on repeated calls
       (see ``tests/runtime/test_spawn_hitl_shared_registry.py`` for why
       that guard exists).
    3. **``LeafExecutorFactory.prepare`` (``runtime/factory.py``, once per
       hop)** — the actual tool assembly happens here, not in ``Agent``:
       calls ``agent.attach_default_hooks()`` first, then
       ``agent.resolve_tools()`` (inline tools + ``tool_registry``),
       merges in MCP tools, a spill-reader tool if paging is active, and
       whatever ``ctx.tool_assemblers`` contribute (spawn/peer/handoff
       tools — the factory itself stays blind to which collaboration
       capabilities exist, per ``compose.py``'s ``hop_tool_assemblers``
       seam). It then builds the system prompt (``build_system_prompt``),
       optionally a ``ContextManager`` (``build_context_manager``), and
       finally a ``PlanExecutor`` or ``ReactiveLoop`` depending on
       ``effective_mode``.

    fork/spawn/peer derivation (``_fork``, ``fork_as_spawn``,
    ``fork_as_peer``) always happens *before* step 3 for the child — a
    forked ``Agent`` re-enters this same sequence from step 2 onward on its
    own hop, it does not inherit an already-wired ``HookRegistry`` unless
    the fork explicitly overrides ``hooks=`` (see ``_runtime_overrides``).
    """

    def __init__(
        self,
        name: str = "",
        *,
        system_prompt: str | None = None,
        tools: Sequence[Tool] | None = None,
        mode: ExecutionMode | None = None,
        budget: HardBudget | None = None,
        workflow: Workflow | None = None,
        allow_replan: bool = True,
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
        if mode is not None:
            cfg.mode = mode
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
        self._bind_invariants(workflow=workflow, allow_replan=allow_replan)

    @classmethod
    def _from_config(cls, config: AgentConfig) -> Agent:
        """Private alternate constructor for forks: assemble the Agent around an
        already-built config instead of re-threading 30 keyword arguments."""
        self = cls.__new__(cls)
        self.config = config
        self._bind_invariants()
        return self

    def _bind_invariants(self, workflow: Workflow | None = None, allow_replan: bool = True) -> None:
        """Constructor invariants, shared by both construction paths."""
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
        self._plan_event_log: EventLog | None = None
        self._plan_checkpoint_store: CheckpointStore | None = None

        # Resolve workflow eagerly
        if workflow is not None:
            from prodagent.plan.workflow import Workflow as _Workflow

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
        """One conversational turn as an event stream. Session-scoped: the
        session allocates (or resumes) the run identity, the transcript folds
        back into the session when a terminal event lands, and the session
        persists under optimistic versioning — two concurrent turns on one
        session conflict loudly instead of last-write-wins."""
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

        # A consumer that abandons this stream mid-run leaves the turn RUNNING
        # on disk — deliberately the same durable state a hard crash produces,
        # because an abandoned run can be mid-tool-call and is NOT cleanly
        # suspendable (a clean suspend goes through RunSuspendedEvent at a
        # step boundary). Both load paths resume such a turn identically; see
        # _load_suspended_turn.
        async for event in drive_stream(
            self,
            message,
            run_id=self._tape_prefixed(run_id),
            forced_mode=resolved_mode,
            initial_messages=messages,
        ):
            if isinstance(event, (RunCompletedEvent, RunFailedEvent, RunSuspendedEvent)):
                # Fold the finished transcript back and persist before the
                # consumer sees the terminal event — a crash right after
                # still leaves a resumable session on disk.
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
        """Stream-and-settle convenience over ``chat_stream`` — reduces the
        stream to its terminal run (a synthetic FAILED one if the stream
        somehow ends bare)."""
        if message is None and not resume:
            raise ValueError(
                "chat() requires a message (or resume=True with an explicit session_id). "
                "For the visual playground, run the `prodagent` CLI instead."
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
        """Answer an approval request this agent's run is parked on. The
        decision lands in the store; resumption happens by re-driving the
        session (``resume=True``) — no in-process waiter is woken, which is
        what lets the deciding human be on another machine."""
        from prodagent.hooks.approval import ApprovalDecision

        gate = self._find_approval_gate()
        if gate is None:
            raise UnknownApprovalError(
                "no ApprovalGate is wired to this agent — cannot submit approval",
                request_id=request_id,
            )
        await gate.submit_decision(request_id, ApprovalDecision(decision), approver_id=approver_id)

    def _find_approval_gate(self) -> ApprovalProvider | None:
        """Locate the approval provider in wiring order: the bus's typed
        slot first, then an explicitly configured one."""
        from prodagent.hooks.approval import ApprovalProvider

        # Idempotent wire-first: what a probe sees is what a run would use.
        self.attach_default_hooks()
        hooks = self.config.hooks
        if hooks is not None:
            gate = hooks.require(ApprovalProvider)
            if gate is not None:
                return cast("ApprovalProvider", gate)
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
        # SUSPENDED is the graceful resumable state. RUNNING is tolerated for
        # hard crashes (kill -9 leaves no chance to suspend); a graceful
        # stream close suspends the turn instead (see chat_stream).
        if session.last_turn is None or session.last_turn.state not in (
            RunState.SUSPENDED,
            RunState.RUNNING,
        ):
            raise PlanAlreadyCompletedError(
                session.last_turn.run_id if session.last_turn else f"<{session_id}>"
            )
        return session, session.last_turn.run_id, session.last_turn.mode

    @staticmethod
    def _tape_prefixed(run_id: str) -> str:
        """Tape attribution for member turns: inside a multi-agent root
        scope, the session's turn id gains the ``<root>::`` prefix — the
        convention spawned children already follow, so one catalog entry
        holds the whole multi-agent run. Deterministic on resume (the same prefix
        derives from the same session id)."""
        from prodagent.base.run_context import current_tape_root

        root = current_tape_root()
        if root and not run_id.startswith(f"{root}::"):
            return f"{root}::{run_id}"
        return run_id

    async def _begin_chat_turn(
        self,
        message: str,
        session_id: str,
        mode: ExecutionMode | None,
    ) -> tuple[ConversationSession, str, ExecutionMode, MessageList]:
        """Open a fresh turn: allocate the run id (a SUSPENDED predecessor
        is resumed instead — see ``start_turn``), guard against an orphan
        checkpoint stealing the id, and persist the session before any work
        starts, so a crash mid-turn finds a resumable record."""
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

    def _ensure_checkpoint_resolved(self) -> CheckpointStore | None:
        from prodagent.runtime.compose import resolve_checkpoint

        self.config.checkpoint = resolve_checkpoint(self.framework_config, self.config.checkpoint)
        return self.config.checkpoint

    def _ensure_session_store_resolved(self) -> SessionStore:
        from prodagent.runtime.compose import resolve_session_store

        self._session_store = resolve_session_store(self.framework_config, self._session_store)
        return self._session_store

    def ensure_plan_event_log_fallback(self) -> EventLog:
        """PLAN_FIRST's DAG state always needs a working event log — unlike
        ``ctx.event_log`` (``None`` is a valid REACTIVE default), a bare
        profile still gets a real (in-process) store here, cached on the
        agent so repeated hops/resumes within the same process share it."""
        if self._plan_event_log is None:
            from prodagent.backends.factory import in_memory_event_log

            self._plan_event_log = in_memory_event_log()
        return self._plan_event_log

    def ensure_plan_checkpoint_fallback(self) -> CheckpointStore:
        """Counterpart to :meth:`ensure_plan_event_log_fallback` for the
        checkpoint side of PLAN_FIRST's persistence pair."""
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
            mode=self.config.mode,
            constraints=list(self.config.constraints),
            budget=self.config.budget,
            tools_schema=[t.schema for t in self.config.tools],
            max_replans=self.config.max_replans,
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

    def _runtime_overrides(self, runtime: ParentRuntime) -> dict[str, Any]:
        """The field-replacement set a fork takes from the parent's wiring —
        exactly the ParentRuntime subset (budget, stores, llm, hooks), never
        the parent's per-hop state."""
        return {
            "llm": runtime.llm,
            "hooks": runtime.hooks,
            "framework": runtime.framework_config,
            "constraints": list(runtime.constraints),
            "budget": runtime.budget,
            "checkpoint": runtime.checkpoint,
            "event_log": runtime.event_log,
            "spawn_accumulator": runtime.accumulator,
        }

    def fork_as_peer(
        self,
        parent: Agent,
        parent_run_id: str | None,
        *,
        checkpoint: CheckpointStore | None = None,
        event_log: EventLog | None = None,
    ) -> Agent:
        """Fork this agent as the next link of a peer chain: the fork runs
        under the *parent's* wiring (hooks, extensions, stores) but keeps
        its own peers — the chain can continue past it."""
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
        # A peer runs under the *parent's* wiring, keeping its own peers.
        forked = self._fork(
            **self._runtime_overrides(runtime),
            extensions=list(parent.config.extensions),
            injectors=list(parent.config.injectors),
            checkers=list(parent.config.checkers),
            event_handlers=list(parent.config.event_handlers),
            mcp=list(parent.config.mcp),
            peers=list(self.config.peers),
        )
        forked._hooks_wired = parent._hooks_wired
        return forked

    def fork_as_spawn(self, runtime: ParentRuntime) -> Agent:
        """Fork as a spawned child: takes the parent's wiring wholesale —
        a child has no peers of its own to preserve."""
        return self._fork(
            **self._runtime_overrides(runtime),
            extensions=list(self.config.extensions),
            injectors=list(self.config.injectors),
            checkers=list(self.config.checkers),
            event_handlers=list(self.config.event_handlers),
            mcp=list(self.config.mcp),
        )

    # -- Properties -------------------------------------------------------

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def mode(self) -> ExecutionMode:
        return self.config.mode

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
