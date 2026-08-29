"""MemoryHooks — wire a MemoryManager onto the hook lifecycle."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from prodagent.kernel.bus import HookRegistry


def _wrap_recall(recall: Callable[..., Any]) -> Callable[..., Any]:
    # Adapt recall(query, domain=None) sync-or-async into async **kwargs injector.

    async def _injector(**kw: Any) -> Any:
        result = recall(kw.get("query", ""))
        if inspect.iscoroutine(result):
            result = await result
        return result

    return _injector


class MemoryHooks:
    """The memory cartridge: expose the manager on the bus's typed slot,
    inject its recall at CONTEXT_INJECTOR (L2), run classification at
    SESSION_END — attach() is the whole wiring, in bundle form."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    @property
    def memory_manager(self) -> Any:
        """Public accessor — keeps Agent from reaching into ``_manager``."""
        return self._manager

    def attach(self, hooks: HookRegistry) -> None:
        """Wire every memory touchpoint in one move — duck-typed on the
        manager so any MemoryProvider-shaped object plugs in the same way."""
        from prodagent.cognition.memory import MemoryProvider
        from prodagent.kernel.bus import HookEvent, InjectionPoint

        hooks.provide(MemoryProvider, self._manager)

        attach = getattr(self._manager, "attach_hooks", None)
        if callable(attach):
            attach(hooks)
        if callable(getattr(self._manager, "recall", None)):
            hooks.register_injector(
                InjectionPoint.CONTEXT_INJECTOR, _wrap_recall(self._manager.recall)
            )
        if callable(getattr(self._manager, "classify", None)):
            hooks.register_event(HookEvent.SESSION_END, self._manager.classify)
