"""Agent observability — audit spans and exporters."""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from prodagent.core.observability import AgentSpan as AgentSpan

if TYPE_CHECKING:
    from prodagent.ports.span import SpanExporter

logger = logging.getLogger(__name__)


def _new_trace_id() -> str:
    return uuid.uuid4().hex


class PassthroughScrubber:
    """No-op redaction — the default. Bring your own scrubber (``scrub`` /
    ``scrub_any``) and pass it as ``AuditLogger(scrubber=...)``."""

    def scrub(self, value: Any) -> Any:
        return value

    def scrub_any(self, value: Any) -> Any:
        return value


def _new_span_id() -> str:
    return os.urandom(8).hex()


class LogExporter:
    """Structured JSON to the Python logging system. Zero dependencies."""

    async def export(self, span: AgentSpan) -> None:
        if span.error:
            logger.error("AUDIT %s", span.to_log_line())
        else:
            logger.info("AUDIT %s", span.to_log_line())

    async def shutdown(self) -> None:
        pass


class AuditLogger:
    """Structured audit sink; pass a custom ``scrubber=`` to opt into redaction."""

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
            self._exporter = LogExporter()
        return self._exporter

    async def record(self, span: AgentSpan) -> None:
        if not self._force_log_unsampled and not span.sampled and not span.error:
            return

        scrubber = self._scrubber
        scrubbed = replace(
            span,
            input_payload=scrubber.scrub(span.input_payload),
            output=scrubber.scrub_any(span.output),
            llm_reasoning=scrubber.scrub_any(span.llm_reasoning),
        )
        await self._resolved_exporter().export(scrubbed)

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

    async def shutdown(self) -> None:
        if self._exporter is not None:
            await self._exporter.shutdown()

    def _is_sampled(self, trace_id: str) -> bool:
        if self._sample_rate >= 1.0:
            return True
        if self._sample_rate <= 0.0:
            return False
        bucket = int(trace_id[:8], 16) % 10_000 / 10_000
        return bucket < self._sample_rate
