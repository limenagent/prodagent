"""SpanExporter port — sink for audit spans (decision snapshots)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from prodagent.base.observability import AgentSpan


@runtime_checkable
class SpanExporter(Protocol):
    """Async like every other store port — an OTLP/DB-backed exporter must
    never block the event loop from inside hook dispatch."""

    async def export(self, span: AgentSpan) -> None:
        """Sink one span. Called from inside hook dispatch — must not raise
        into the bus (implementations swallow or log their own failures)."""
        ...

    async def shutdown(self) -> None: ...
