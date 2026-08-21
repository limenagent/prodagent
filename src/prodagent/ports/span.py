"""SpanExporter port — sink for audit spans (decision snapshots)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from prodagent.core.observability import AgentSpan


@runtime_checkable
class SpanExporter(Protocol):
    """Async like every other store port — an OTLP/DB-backed exporter must
    never block the event loop from inside hook dispatch."""

    async def export(self, span: AgentSpan) -> None: ...

    async def shutdown(self) -> None: ...
