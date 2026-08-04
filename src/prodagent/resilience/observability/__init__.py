"""Observability — audit spans, exporters, scrubber."""

from typing import Any

from prodagent.resilience.observability.audit import (
    AgentSpan,
    AuditLogger,
)
from prodagent.resilience.observability.drift import Drift, DriftDetector, DriftReport
from prodagent.resilience.observability.scrubber import DefaultScrubber, PassthroughScrubber

__all__ = [
    "AgentSpan",
    "AuditLogger",
    "OtelSpanExporter",
    "Drift",
    "DriftDetector",
    "DriftReport",
    "PassthroughScrubber",
    "DefaultScrubber",
]


def __getattr__(name: str) -> Any:  # PEP 562
    if name == "OtelSpanExporter":
        from prodagent.resilience.observability.otel_exporter import OtelSpanExporter

        return OtelSpanExporter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
