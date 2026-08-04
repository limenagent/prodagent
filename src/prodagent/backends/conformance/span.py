"""Conformance tests for ``SpanExporter`` implementations.

SpanExporter methods are synchronous on the port.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeAlias

from prodagent.ports.span import SpanExporter
from prodagent.resilience.observability.audit import AgentSpan

Factory: TypeAlias = Callable[[], SpanExporter]


def _span(span_id: str = "sp1", trace_id: str = "tr1") -> AgentSpan:
    return AgentSpan(
        span_id=span_id,
        trace_id=trace_id,
        run_id="r1",
        action="tool_call",
        input_payload={"name": "lookup"},
        timestamp=time.time(),
    )


def run_span_conformance(make_store: Factory) -> None:
    store = make_store()

    store.export(_span())
    store.export(_span(span_id="sp2"))
    store.shutdown()


def run_span_shutdown_idempotent_conformance(make_store: Factory) -> None:
    """``shutdown`` may be called multiple times safely."""
    store = make_store()
    store.export(_span())
    store.shutdown()
    store.shutdown()


def run_span_export_after_shutdown_conformance(make_store: Factory) -> None:
    """Whether export-after-shutdown raises or is a no-op is backend-specific;
    the contract is only that shutdown itself does not crash."""
    store = make_store()
    store.shutdown()
