"""SpanExporter port — sink for audit spans (decision snapshots)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from prodagent.resilience.observability.audit import AgentSpan


@runtime_checkable
class SpanExporter(Protocol):
    def export(self, span: AgentSpan) -> None: ...

    def shutdown(self) -> None: ...
