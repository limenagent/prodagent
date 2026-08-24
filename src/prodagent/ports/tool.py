"""Tool port — callable tool surface stored by ToolRegistry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from prodagent.core.aliases import ToolSchema
    from prodagent.kernel.types import ToolMeta, ToolName, ToolResult


@runtime_checkable
class Tool(Protocol):
    """Callable tool surface stored by ``ToolRegistry``, invoked by ``ToolDispatcher``."""

    @property
    def name(self) -> ToolName: ...

    @property
    def meta(self) -> ToolMeta: ...

    @property
    def schema(self) -> ToolSchema: ...

    async def __call__(self, **kwargs: Any) -> ToolResult: ...
