"""Agent lifecycle hooks — tri-protocol bus."""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from prodagent.hooks.checkpoint import BlockingResult, CheckPoint, FailurePolicy, InjectionPoint
from prodagent.hooks.events import HookEvent

logger = logging.getLogger(__name__)

EventHandler = Callable[..., None | Awaitable[None]]
CheckHandler = Callable[..., BlockingResult | None | Awaitable[BlockingResult | None]]
InjectorHandler = Callable[..., Any | Awaitable[Any]]


def _is_structured_veto(exc: BaseException) -> bool:
    from prodagent.core.exceptions import SECURITY_VETO_EXCEPTIONS

    return isinstance(exc, SECURITY_VETO_EXCEPTIONS)


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


class HookRegistry:
    """Three-protocol event bus: event / check / inject."""

    def __init__(self, *, failure_policy: FailurePolicy = FailurePolicy.FAIL_CLOSED) -> None:
        self._event_handlers: dict[HookEvent, list[tuple[int, EventHandler]]] = {
            e: [] for e in HookEvent
        }
        self._check_handlers: dict[CheckPoint, list[tuple[int, CheckHandler]]] = {
            c: [] for c in CheckPoint
        }
        self._injectors: dict[InjectionPoint, list[tuple[int, InjectorHandler]]] = {
            p: [] for p in InjectionPoint
        }
        self._failure_policy: FailurePolicy = failure_policy
        self._attached_extensions: set[int] = set()

    @staticmethod
    def _insert(bucket: list[tuple[int, Any]], priority: int, handler: Any) -> None:
        bucket.append((priority, handler))
        bucket.sort(key=lambda pair: -pair[0])

    def register_event(self, event: HookEvent, handler: EventHandler, *, priority: int = 0) -> None:
        self._insert(self._event_handlers[event], priority, _ensure_async(handler))

    def register_all_events(self, handler: EventHandler, *, priority: int = 0) -> None:
        wrapped = _ensure_async(handler)
        for event in HookEvent:
            self._insert(self._event_handlers[event], priority, wrapped)

    def register_checker(
        self, point: CheckPoint, checker: CheckHandler, *, priority: int = 0
    ) -> None:
        self._insert(self._check_handlers[point], priority, _ensure_async(checker))

    def register_injector(
        self, point: InjectionPoint, injector: InjectorHandler, *, priority: int = 0
    ) -> None:
        self._insert(self._injectors[point], priority, _ensure_async(injector))

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

    @staticmethod
    def _handlers_for(bucket: list[tuple[int, Any]]) -> list[Any]:
        return [h for _, h in bucket]

    async def _dispatch_event(
        self,
        handlers: list[EventHandler],
        payload: dict[str, Any],
        point_name: str,
    ) -> None:
        if not handlers:
            return

        async def run(h: EventHandler) -> None:
            try:
                result = h(**payload)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                if _is_structured_veto(exc):
                    raise
                logger.error(
                    "Event observer %s at %s raised %s: %s",
                    getattr(h, "__qualname__", repr(h)),
                    point_name,
                    type(exc).__name__,
                    exc,
                    exc_info=exc,
                )

        await asyncio.gather(*(run(h) for h in handlers))

    async def _dispatch_check(
        self,
        handlers: list[CheckHandler],
        payload: dict[str, Any],
        point_name: str,
    ) -> BlockingResult | None:
        for h in handlers:
            try:
                result = h(**payload)
                if inspect.isawaitable(result):
                    r = await result
                else:
                    r = result
            except Exception as exc:
                if _is_structured_veto(exc):
                    raise
                veto = self._handle_checker_failure(point_name, h, exc)
                if veto is not None:
                    return veto
                continue

            interpreted = self._interpret_check_result(point_name, h, r)
            if interpreted is not None:
                return interpreted
        return None

    async def _dispatch_collect(
        self,
        handlers: list[InjectorHandler],
        payload: dict[str, Any],
        point_name: str,
    ) -> list[Any]:
        if not handlers:
            return []

        async def run(h: InjectorHandler) -> Any:
            try:
                return await h(**payload)
            except Exception as exc:
                if _is_structured_veto(exc):
                    raise
                logger.warning(
                    "Injector %s at %s raised %s: %s",
                    getattr(h, "__qualname__", repr(h)),
                    point_name,
                    type(exc).__name__,
                    exc,
                    exc_info=exc,
                )
                await self._fire_injection_failed(point_name, h, exc)
                return None

        batch = await asyncio.gather(*(run(h) for h in handlers))
        return [r for r in batch if r is not None]

    async def fire(self, event: HookEvent, **data: Any) -> None:
        # Passes event_name=event.value so all-events observers can self-dispatch.
        payload = {"event_name": event.value, **data}
        handlers = self._handlers_for(self._event_handlers[event])
        await self._dispatch_event(handlers, payload, event.value)

    async def check_blocking(self, point: CheckPoint, **data: Any) -> BlockingResult:
        handlers = self._handlers_for(self._check_handlers[point])
        if not handlers:
            return BlockingResult(blocked=False)

        veto = await self._dispatch_check(handlers, data, point.value)
        return veto if veto is not None else BlockingResult(blocked=False)

    async def collect(self, point: InjectionPoint, **data: Any) -> list[Any]:
        handlers = self._handlers_for(self._injectors[point])
        if not handlers:
            return []
        return await self._dispatch_collect(handlers, data, point.value)

    def _handle_checker_failure(
        self,
        point_name: str,
        checker: CheckHandler,
        exc: Exception,
    ) -> BlockingResult | None:
        name = getattr(checker, "__qualname__", repr(checker))
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
            f"Checker {getattr(checker, '__qualname__', checker)!r} at {point_name} "
            f"returned unexpected type {type(check_result).__name__}; "
            "expected BlockingResult or None."
        )

    async def _fire_injection_failed(
        self, point_name: str, injector: InjectorHandler, exc: Exception
    ) -> None:
        payload = {
            "event_name": HookEvent.INJECTION_FAILED.value,
            "point": point_name,
            "injector": getattr(injector, "__qualname__", repr(injector)),
            "error": str(exc),
        }
        handlers = self._handlers_for(self._event_handlers[HookEvent.INJECTION_FAILED])
        await self._dispatch_event(handlers, payload, HookEvent.INJECTION_FAILED.value)

    def has_event_handlers(self, event: HookEvent) -> bool:
        return bool(self._event_handlers[event])

    def has_check_handlers(self, point: CheckPoint) -> bool:
        return bool(self._check_handlers[point])

    def has_injector_handlers(self, point: InjectionPoint) -> bool:
        return bool(self._injectors[point])

    def event_handlers(self, event: HookEvent) -> list[EventHandler]:
        return self._handlers_for(self._event_handlers[event])
