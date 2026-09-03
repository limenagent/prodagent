"""The tri-protocol bus — the kernel's one seam to the outside world.

Three channels with deliberately different semantics:

- **fire** (event): observe. Fan-out, concurrent, failures logged not raised.
- **check** (gate): intervene. Serial, ordered, a veto stops the run; a
  checker that raises fails closed.
- **collect** (injection): contribute. Fan-out, results gathered, failures
  degrade to ``None`` and leave an ``injection.failed`` trace.

Everything the framework does around the loop — observability, approval,
memory recall, learning — plugs in here. The loop itself never knows their
names.

The module has two floors: the generic mount/dispatch plumbing
(:class:`_Pipeline` / :class:`_Stage` / :class:`_StageMode` — zero domain
knowledge, three dispatch shapes) and :class:`HookRegistry`, which owns the
domain interpretation of each protocol — what a veto looks like, how a
failed checker degrades, how a failed injector is traced. The plumbing used
to be its own module; it has exactly one consumer (the registry), so it
lives here — one concept, one module.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "HookEvent",
    "fire",
    "fire_checkpoint_failed",
    "save_and_fire_checkpoint",
    "BlockingResult",
    "FailurePolicy",
    "Gate",
    "InjectionPoint",
    "HookRegistry",
]


# ── Generic dispatch plumbing — mount + three shapes ─────────────────────────


class _StageMode(StrEnum):
    """Dispatch shape for a mounted stage — see :class:`_Pipeline`."""

    OBSERVE = "observe"
    VETO = "veto"
    GATHER = "gather"


def _ensure_async(handler: Any) -> Any:
    if inspect.iscoroutinefunction(handler):
        return handler

    @functools.wraps(handler)
    async def _async_wrapper(**kw: Any) -> Any:
        result = handler(**kw)
        if inspect.iscoroutine(result):
            result = await result
        return result

    return _async_wrapper


@dataclass
class _Stage:
    """One mounted capability. ``fn`` may be sync or async — normalized on construction."""

    name: str
    fn: Callable[..., Any]
    mode: _StageMode
    priority: int = 0

    def __post_init__(self) -> None:
        self.fn = _ensure_async(self.fn)


IsVeto = Callable[[BaseException], bool]
OnError = Callable[[_Stage, Exception], Awaitable[Any | None]]
Interpret = Callable[[_Stage, Any], Any | None]


def _default_is_veto(_exc: BaseException) -> bool:
    return False


@dataclass
class _Pipeline:
    """Per-point mount registry plus the three generic dispatch shapes.

    Three modes, matching what the bus actually does (verified via
    ``tests/hooks/test_hooks_fluent_semantics.py``'s concurrency-timing
    assertions — NOT a four-mode taxonomy an earlier plan draft assumed):

    - **OBSERVE**: concurrent (``asyncio.gather``), fire-and-forget. Non-veto
      exceptions are logged and swallowed; a caller-supplied ``is_veto``
      predicate decides which exceptions re-raise instead.
    - **VETO**: sequential, first-signal-wins short-circuit. The caller supplies
      ``interpret`` (turn a stage's return value into a stop-and-return-this
      outcome, or ``None`` to keep going) and ``on_error`` (same shape, for
      exceptions) — this primitive only owns the loop and the short-circuit.
    - **GATHER**: concurrent (``asyncio.gather``), collect every non-``None``
      result into a list, preserving mount/priority order regardless of
      completion order. This is deliberately *not* TRANSFORM (sequential
      value-threading): collect() runs every stage against the same input
      independently, it does not chain outputs into inputs. Messaging's
      ``coordination/messaging/pipeline.py::Pipeline`` is structurally similar
      but its per-exception-type control flow (dedupe / lenient-reject /
      strict-reject, each with different dead-letter side effects) doesn't fit
      any of these three runners without heavy parameterization, so it stays a
      separate, un-migrated implementation — see that module's docstring.
    """

    _stages: dict[Any, list[_Stage]] = field(default_factory=dict)

    def mount(self, point: Any, stage: _Stage) -> None:
        """Mount a stage at ``point``, stable-sorted by descending priority."""
        bucket = self._stages.setdefault(point, [])
        bucket.append(stage)
        bucket.sort(key=lambda s: -s.priority)

    def stages(self, point: Any) -> list[_Stage]:
        return list(self._stages.get(point, ()))

    def has_stages(self, point: Any) -> bool:
        return bool(self._stages.get(point))

    def _require_mode(self, point: Any, stages: list[_Stage], mode: _StageMode) -> None:
        # A point has one dispatch semantic; a mismatched mount is a wiring bug
        # that would otherwise run a veto-checker as fire-and-forget (or worse).
        for stage in stages:
            if stage.mode is not mode:
                raise TypeError(
                    f"stage {stage.name!r} at {point!r} mounted as "
                    f"{stage.mode.value} but dispatched as {mode.value}"
                )

    async def observe(
        self,
        point: Any,
        payload: dict[str, Any],
        *,
        is_veto: IsVeto = _default_is_veto,
    ) -> None:
        """OBSERVE: run every stage at ``point`` concurrently, swallowing non-veto errors."""
        stages = self.stages(point)
        if not stages:
            return
        self._require_mode(point, stages, _StageMode.OBSERVE)

        async def run(stage: _Stage) -> None:
            try:
                await stage.fn(**payload)
            except Exception as exc:
                if is_veto(exc):
                    raise
                logger.error(
                    "Stage %s at %s raised %s: %s",
                    stage.name,
                    point,
                    type(exc).__name__,
                    exc,
                    exc_info=exc,
                )

        await asyncio.gather(*(run(s) for s in stages))

    async def gather(
        self,
        point: Any,
        payload: dict[str, Any],
        *,
        is_veto: IsVeto = _default_is_veto,
        on_error: OnError | None = None,
    ) -> list[Any]:
        """GATHER: run every stage at ``point`` concurrently, collect non-``None`` results
        in mount order (independent of completion order)."""
        stages = self.stages(point)
        if not stages:
            return []
        self._require_mode(point, stages, _StageMode.GATHER)

        async def run(stage: _Stage) -> Any:
            try:
                return await stage.fn(**payload)
            except Exception as exc:
                if is_veto(exc):
                    raise
                if on_error is not None:
                    await on_error(stage, exc)
                return None

        batch = await asyncio.gather(*(run(s) for s in stages))
        return [r for r in batch if r is not None]

    async def veto(
        self,
        point: Any,
        payload: dict[str, Any],
        *,
        interpret: Interpret,
        is_veto: IsVeto = _default_is_veto,
        on_error: OnError | None = None,
    ) -> Any | None:
        """VETO: sequential, first non-``None`` outcome (from ``interpret`` or ``on_error``)
        short-circuits and is returned. ``None`` after every stage means no veto."""
        stages = self.stages(point)
        self._require_mode(point, stages, _StageMode.VETO)
        for stage in stages:
            try:
                result = await stage.fn(**payload)
            except Exception as exc:
                if is_veto(exc):
                    raise
                outcome = await on_error(stage, exc) if on_error is not None else None
                if outcome is not None:
                    return outcome
                continue
            outcome = interpret(stage, result)
            if outcome is not None:
                return outcome
        return None

    def describe(self) -> str:
        """Human-readable mount map — debugging what got wired where."""
        parts = [
            f"{point}: [{', '.join(f'{s.name}({s.mode.value})' for s in stages)}]"
            for point, stages in self._stages.items()
            if stages
        ]
        return " | ".join(parts) or "(empty)"


# ── Event vocabulary ──────────────────────────────────────────────────────────


class HookEvent(StrEnum):
    SESSION_START = "session.start"
    SESSION_END = "session.end"

    LOOP_START = "loop.start"
    LOOP_END = "loop.end"

    CONTEXT_BUILD = "context.build"
    MEMORY_RECALL = "memory.recall"
    MEMORY_CLASSIFY = "memory.classify"
    INJECTION_FAILED = "injection.failed"  # injector raised — degraded, must leave a trace
    CHECKPOINT_FAILED = (
        "checkpoint.failed"  # checkpoint write raised — degraded, must leave a trace
    )

    SKILLS_READY = "skills.ready"

    ROUND_START = "round.start"
    LLM_REQUEST = "llm.request"
    THINK = "llm.think"

    TOOL_CALL = "tool.call"
    APPROVAL_REQUEST = "approval.request"
    TOOL_RESULT = "tool.result"

    PLAN_READY = "plan.ready"
    PLAN_REPLANNED = "plan.replanned"

    NODE_STARTED = "node.started"
    NODE_COMPLETED = "node.completed"
    NODE_FAILED = "node.failed"

    SKILL_LOAD = "skill.load"
    AGENT_SPAWN = "agent.spawn"
    AGENT_RESULT = "agent.result"
    PEER_HANDOFF = "peer.handoff"

    LEARNING_SYNTHESIZE = "learning.synthesize"

    TOKEN_UPDATE = "budget.token_update"
    RUN_COMPLETE = "run.complete"
    RUN_FAILED = "run.failed"


# ── Gate vocabulary ───────────────────────────────────────────────────────────


@dataclass
class BlockingResult:
    blocked: bool = False
    reason: str | None = None


class FailurePolicy(StrEnum):
    FAIL_OPEN = "fail_open"
    FAIL_CLOSED = "fail_closed"


class Gate(StrEnum):
    TOOL_CALL = "gate.tool_call"
    PLAN_APPROVAL = "gate.plan_approval"

    # L1-L5 security pipeline gates
    SESSION_START = "gate.session_start"
    CONTEXT_BUILD = "gate.context_build"
    TOOL_RESULT = "gate.tool_result"
    RUN_COMPLETE = "gate.run_complete"

    APPROVAL_REQUEST = "gate.approval_request"
    AGENT_HANDOFF = "gate.agent_handoff"
    DOCUMENT_ADD = "gate.document_add"


class InjectionPoint(StrEnum):
    CONTEXT_INJECTOR = "inject.context"


# ── Registry mechanics ────────────────────────────────────────────────────────


EventHandler = Callable[..., None | Awaitable[None]]
CheckHandler = Callable[..., BlockingResult | None | Awaitable[BlockingResult | None]]
InjectorHandler = Callable[..., Any | Awaitable[Any]]


def _is_structured_veto(exc: BaseException) -> bool:
    # Security vetoes pierce even observe/gather paths: a permission denial
    # must abort the run, never be logged-and-swallowed as observer noise.
    from prodagent.base.errors import SECURITY_VETO_EXCEPTIONS

    return isinstance(exc, SECURITY_VETO_EXCEPTIONS)


def _name_of(handler: Any) -> str:
    return getattr(handler, "__qualname__", repr(handler))


class HookRegistry:
    """Three-protocol event bus: event / check / inject.

    Mount/dispatch plumbing (priority ordering, sync-or-async normalization,
    concurrent vs. sequential running) is :class:`_Pipeline` above; this class
    owns only the domain interpretation of each protocol — what a veto looks
    like, how a failed checker degrades, how a failed injector is traced."""

    def __init__(self, *, failure_policy: FailurePolicy = FailurePolicy.FAIL_CLOSED) -> None:
        self._pipeline: _Pipeline = _Pipeline()
        self._failure_policy: FailurePolicy = failure_policy
        self._attached_extensions: set[int] = set()
        self._capabilities: dict[type, Any] = {}

    def register_event(self, event: HookEvent, handler: EventHandler, *, priority: int = 0) -> None:
        self._pipeline.mount(
            event,
            _Stage(name=_name_of(handler), fn=handler, mode=_StageMode.OBSERVE, priority=priority),
        )

    def register_all_events(self, handler: EventHandler, *, priority: int = 0) -> None:
        name = _name_of(handler)
        for event in HookEvent:
            self._pipeline.mount(
                event, _Stage(name=name, fn=handler, mode=_StageMode.OBSERVE, priority=priority)
            )

    def register_checker(self, point: Gate, checker: CheckHandler, *, priority: int = 0) -> None:
        self._pipeline.mount(
            point,
            _Stage(name=_name_of(checker), fn=checker, mode=_StageMode.VETO, priority=priority),
        )

    def register_injector(
        self, point: InjectionPoint, injector: InjectorHandler, *, priority: int = 0
    ) -> None:
        self._pipeline.mount(
            point,
            _Stage(name=_name_of(injector), fn=injector, mode=_StageMode.GATHER, priority=priority),
        )

    def provide(self, capability: type, impl: Any) -> None:
        """Declare a typed capability (an ApprovalProvider, a MemoryProvider).

        The replacement for scanning extension bags with isinstance/hasattr:
        attachers declare what they carry, consumers require by type."""
        self._capabilities[capability] = impl

    def require(self, capability: type) -> Any | None:
        """Look up a declared capability; ``None`` when nobody provides it."""
        return self._capabilities.get(capability)

    def attach_extension(self, ext: Any) -> bool:
        """Attach an ``extensions=`` extension idempotently."""
        key = id(ext)
        if key in self._attached_extensions:
            return False
        attach = getattr(ext, "attach", None)
        if not callable(attach):
            raise TypeError(f"Extension {ext!r} passed to extensions= has no attach(hooks) method")
        attach(self)
        self._attached_extensions.add(key)
        return True

    async def fire(self, event: HookEvent, **data: Any) -> None:
        # Passes event_name=event.value so all-events observers can self-dispatch.
        payload = {"event_name": event.value, **data}
        await self._pipeline.observe(event, payload, is_veto=_is_structured_veto)

    async def check_blocking(self, point: Gate, **data: Any) -> BlockingResult:
        veto = await self._pipeline.veto(
            point,
            data,
            interpret=lambda stage, result: self._interpret_check_result(
                point.value, stage.fn, result
            ),
            is_veto=_is_structured_veto,
            on_error=lambda stage, exc: self._handle_checker_failure_async(
                point.value, stage.fn, exc
            ),
        )
        return veto if veto is not None else BlockingResult(blocked=False)

    async def collect(self, point: InjectionPoint, **data: Any) -> list[Any]:
        return await self._pipeline.gather(
            point,
            data,
            is_veto=_is_structured_veto,
            on_error=lambda stage, exc: self._injector_failed(point.value, stage.fn, exc),
        )

    async def _handle_checker_failure_async(
        self, point_name: str, checker: CheckHandler, exc: Exception
    ) -> BlockingResult | None:
        return self._handle_checker_failure(point_name, checker, exc)

    def _handle_checker_failure(
        self,
        point_name: str,
        checker: CheckHandler,
        exc: Exception,
    ) -> BlockingResult | None:
        name = _name_of(checker)
        if self._failure_policy is FailurePolicy.FAIL_CLOSED:
            logger.error(
                "Checker %s at %s raised %s — fail-closed → veto",
                name,
                point_name,
                type(exc).__name__,
                exc_info=exc,
            )
            return BlockingResult(
                blocked=True,
                reason=f"Checker {name} failed ({type(exc).__name__}: {exc})",
            )
        logger.warning(
            "Checker %s at %s raised %s — fail-open, continuing",
            name,
            point_name,
            type(exc).__name__,
            exc_info=exc,
        )
        return None

    def _interpret_check_result(
        self,
        point_name: str,
        checker: CheckHandler,
        check_result: BlockingResult | None,
    ) -> BlockingResult | None:
        if check_result is None:
            return None
        if isinstance(check_result, BlockingResult):
            return check_result if check_result.blocked else None
        raise TypeError(
            f"Checker {_name_of(checker)!r} at {point_name} "
            f"returned unexpected type {type(check_result).__name__}; "
            "expected BlockingResult or None."
        )

    async def _injector_failed(
        self, point_name: str, injector: InjectorHandler, exc: Exception
    ) -> None:
        logger.warning(
            "Injector %s at %s raised %s: %s",
            _name_of(injector),
            point_name,
            type(exc).__name__,
            exc,
            exc_info=exc,
        )
        # Bypasses self.fire (dispatches straight to the pipeline) so a
        # monkeypatched/wrapped fire can't recurse back into a failing injector.
        payload = {
            "event_name": HookEvent.INJECTION_FAILED.value,
            "point": point_name,
            "injector": _name_of(injector),
            "error": str(exc),
        }
        await self._pipeline.observe(
            HookEvent.INJECTION_FAILED, payload, is_veto=_is_structured_veto
        )

    def has_check_handlers(self, point: Gate) -> bool:
        return self._pipeline.has_stages(point)

    def has_injector_handlers(self, point: InjectionPoint) -> bool:
        return self._pipeline.has_stages(point)

    def event_handlers(self, event: HookEvent) -> list[EventHandler]:
        return [s.fn for s in self._pipeline.stages(event)]

    def check_handlers(self, point: Gate) -> list[CheckHandler]:
        return [s.fn for s in self._pipeline.stages(point)]


# ── Dispatch + checkpoint conveniences ────────────────────────────────────────


async def fire(bus: HookRegistry | None, event: HookEvent, **payload: Any) -> None:
    """Null-safe dispatch — the loop fires whether or not anyone listens."""
    if bus is not None:
        await bus.fire(event, **payload)


async def fire_checkpoint_failed(bus: HookRegistry | None, run: Any, *, was_failed: bool) -> None:
    """Fire CHECKPOINT_FAILED when a save flipped the run's sticky flag.

    "Checkpoint" here means run-state persistence (CheckpointStore snapshot),
    not the Gate enum (hook-lifecycle blocking gate) above.
    """
    if bus is not None and not was_failed and run.checkpoint_failed:
        await bus.fire(
            HookEvent.CHECKPOINT_FAILED,
            run_id=run.run_id,
            turns=run.turn_count,
        )


async def save_and_fire_checkpoint(
    store: Any,
    run: Any,
    bus: HookRegistry | None,
    *,
    expected_version: int | None = None,
) -> None:
    """Save run to checkpoint store and fire CHECKPOINT_FAILED if the save flipped the flag."""
    was_failed = run.checkpoint_failed
    await store.save(
        run,
        expected_version=expected_version
        if expected_version is not None
        else run.checkpoint_version,
    )
    await fire_checkpoint_failed(bus, run, was_failed=was_failed)
