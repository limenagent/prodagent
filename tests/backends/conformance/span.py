"""Conformance tests for ``SpanExporter`` implementations.

SpanExporter methods are async on the port, like every other store port.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeAlias

from prodagent.base.observability import AgentSpan
from prodagent.ports.span import SpanExporter

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


async def run_span_conformance(make_store: Factory) -> None:
    store = make_store()

    await store.export(_span())
    await store.export(_span(span_id="sp2"))
    await store.shutdown()


async def run_span_shutdown_idempotent_conformance(make_store: Factory) -> None:
    """``shutdown`` may be called multiple times safely."""
    store = make_store()
    await store.export(_span())
    await store.shutdown()
    await store.shutdown()


async def run_span_export_after_shutdown_conformance(make_store: Factory) -> None:
    """Whether export-after-shutdown raises or is a no-op is backend-specific;
    the contract is only that shutdown itself does not crash."""
    store = make_store()
    await store.shutdown()
