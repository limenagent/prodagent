"""Agent observability — audit spans and exporters."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any

from prodagent.resilience.observability.scrubber import PassthroughScrubber

if TYPE_CHECKING:
    from prodagent.ports.span import SpanExporter

logger = logging.getLogger(__name__)


def _new_trace_id() -> str:
    return uuid.uuid4().hex


def _new_span_id() -> str:
    return os.urandom(8).hex()


@dataclass
class AgentSpan:
    """Decision snapshot — what happened, where it fits, why the model chose it."""

    span_id: str
    trace_id: str
    run_id: str
    action: str
    input_payload: dict[str, Any]
    timestamp: float

    parent_span_id: str | None = None
    system_prompt_version: str = ""
    retrieved_context: list[str] = field(default_factory=list)
    llm_reasoning: str = ""
    output: Any = None
    error: str | None = None
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    sampled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_log_line(self) -> str:
        return json.dumps(
            {
                "span_id": self.span_id,
                "trace_id": self.trace_id,
                "parent_span_id": self.parent_span_id,
                "run_id": self.run_id,
                "action": self.action,
                "latency_ms": round(self.latency_ms, 1),
                "cost_usd": round(self.cost_usd, 6),
                "error": self.error,
            }
        )


class AuditLogger:
    """Structured audit sink with head-based sampling and PII scrubbing."""

    def __init__(
        self,
        exporter: SpanExporter | None = None,
        *,
        sample_rate: float = 1.0,
        scrubber: Any = None,
        force_log_unsampled: bool = False,
    ) -> None:
        self._exporter = exporter
        self._sample_rate = max(0.0, min(1.0, sample_rate))
        self._scrubber = scrubber or PassthroughScrubber()
        self._force_log_unsampled = force_log_unsampled

    def _resolved_exporter(self) -> SpanExporter:
        if self._exporter is None:
            from prodagent.backends.file.span import LogExporter

            self._exporter = LogExporter()
        return self._exporter

    def record(self, span: AgentSpan) -> None:
        if not self._force_log_unsampled and not span.sampled and not span.error:
            return

        scrubber = self._scrubber
        scrubbed = replace(
            span,
            input_payload=scrubber.scrub(span.input_payload),
            output=scrubber.scrub_any(span.output),
            llm_reasoning=scrubber.scrub_any(span.llm_reasoning),
        )
        self._resolved_exporter().export(scrubbed)

    def span(
        self,
        run_id: str,
        action: str,
        payload: dict[str, Any],
        *,
        root: bool = False,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
    ) -> AgentSpan:
        resolved_trace = trace_id or _new_trace_id()
        resolved_parent = None if root else parent_span_id
        return AgentSpan(
            span_id=_new_span_id(),
            trace_id=resolved_trace,
            parent_span_id=resolved_parent,
            run_id=run_id,
            action=action,
            input_payload=dict(payload),
            timestamp=time.time(),
            sampled=self._is_sampled(resolved_trace),
        )

    def shutdown(self) -> None:
        if self._exporter is not None:
            self._exporter.shutdown()

    def _is_sampled(self, trace_id: str) -> bool:
        if self._sample_rate >= 1.0:
            return True
        if self._sample_rate <= 0.0:
            return False
        bucket = int(trace_id[:8], 16) % 10_000 / 10_000
        return bucket < self._sample_rate
