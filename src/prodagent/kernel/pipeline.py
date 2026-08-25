"""Pipeline — shared mount/dispatch plumbing behind HookRegistry's three protocols.

``HookRegistry`` hand-rolled the same three pieces of plumbing three times: wrap
a sync-or-async handler uniformly, insert it into a priority-sorted bucket per
point, then run the bucket either concurrently (fire-and-forget / collect-all)
or sequentially (first-veto-wins). This module extracts exactly that plumbing
— nothing domain-specific (no ``BlockingResult``, no ``FailurePolicy``, no
``HookEvent``/``Gate``/``InjectionPoint``) lives here; those interpretations
stay in :mod:`prodagent.hooks.registry`, passed in as callbacks.

Three modes, matching what the codebase actually does today (verified via
``tests/hooks/test_hooks_fluent_semantics.py``'s concurrency-timing assertions
— NOT the four-mode taxonomy an earlier plan draft assumed):

- **OBSERVE**: concurrent (``asyncio.gather``), fire-and-forget. Non-veto
  exceptions are logged and swallowed; a caller-supplied ``is_veto`` predicate
  decides which exceptions re-raise instead.
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

__all__ = ["StageMode", "Stage", "Pipeline"]


class StageMode(StrEnum):
    """Dispatch shape for a mounted stage — see module docstring."""

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
class Stage:
    """One mounted capability. ``fn`` may be sync or async — normalized on construction."""

    name: str
    fn: Callable[..., Any]
    mode: StageMode
    priority: int = 0

    def __post_init__(self) -> None:
        self.fn = _ensure_async(self.fn)


IsVeto = Callable[[BaseException], bool]
OnError = Callable[[Stage, Exception], Awaitable[Any | None]]
Interpret = Callable[[Stage, Any], Any | None]


def _default_is_veto(_exc: BaseException) -> bool:
    return False


@dataclass
class Pipeline:
    """Per-point mount registry plus the three generic dispatch shapes."""

    _stages: dict[Any, list[Stage]] = field(default_factory=dict)

    def mount(self, point: Any, stage: Stage) -> None:
        """Mount a stage at ``point``, stable-sorted by descending priority."""
        bucket = self._stages.setdefault(point, [])
        bucket.append(stage)
        bucket.sort(key=lambda s: -s.priority)

    def stages(self, point: Any) -> list[Stage]:
        return list(self._stages.get(point, ()))

    def has_stages(self, point: Any) -> bool:
        return bool(self._stages.get(point))

    def _require_mode(self, point: Any, stages: list[Stage], mode: StageMode) -> None:
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
        self._require_mode(point, stages, StageMode.OBSERVE)

        async def run(stage: Stage) -> None:
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
        self._require_mode(point, stages, StageMode.GATHER)

        async def run(stage: Stage) -> Any:
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
        self._require_mode(point, stages, StageMode.VETO)
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
