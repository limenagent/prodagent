"""Agent observability — audit spans and exporters."""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from prodagent.base.event_log import Event, SpanEventType, spans_stream
from prodagent.base.observability import AgentSpan as AgentSpan
from prodagent.base.run_context import current_event_log

if TYPE_CHECKING:
    from prodagent.ports.observability import EventLog, SpanExporter

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
    """Structured audit sink; pass a custom ``scrubber=`` to opt into redaction.

    A recorded span is a *fact* first: with an event log in reach
    (injected explicitly, or picked up from the run scope the driver opened),
    every span lands once on ``<run_id>#spans`` and the exporter becomes a
    projection cache — deletable and rebuildable via :func:`rebuild_spans`.
    """

    def __init__(
        self,
        exporter: SpanExporter | None = None,
        *,
        sample_rate: float = 1.0,
        scrubber: Any = None,
        force_log_unsampled: bool = False,
        event_log: EventLog | None = None,
    ) -> None:
        self._exporter = exporter
        self._sample_rate = max(0.0, min(1.0, sample_rate))
        self._scrubber = scrubber or PassthroughScrubber()
        self._force_log_unsampled = force_log_unsampled
        self._event_log = event_log

    def _resolved_exporter(self) -> SpanExporter:
        if self._exporter is None:
            self._exporter = LogExporter()
        return self._exporter

    def _resolved_event_log(self) -> EventLog | None:
        if self._event_log is not None:
            return self._event_log
        return current_event_log()

    async def record(self, span: AgentSpan) -> None:
        """Record one span — sampling drops non-error spans (errors always
        through: a failure you sampled away is a failure you can't diagnose),
        and the scrubber's redaction happens here, at the sink, once. The
        fact lands on the WAL before any exporter sees it: a failed export
        costs a cache miss, never a lost span."""
        if not self._force_log_unsampled and not span.sampled and not span.error:
            return

        scrubber = self._scrubber
        scrubbed = replace(
            span,
            input_payload=scrubber.scrub(span.input_payload),
            output=scrubber.scrub_any(span.output),
            llm_reasoning=scrubber.scrub_any(span.llm_reasoning),
        )
        log = self._resolved_event_log()
        if log is not None and scrubbed.run_id:
            try:
                await log.append(
                    Event.make(
                        SpanEventType.SPAN_RECORDED,
                        stream_id=spans_stream(scrubbed.run_id),
                        version=0,
                        span=scrubbed.to_dict(),
                    )
                )
            except Exception:  # noqa: BLE001 — fact recording must not kill the observer
                logger.exception("[spans] failed to record span fact for %s", scrubbed.run_id)
        try:
            await self._resolved_exporter().export(scrubbed)
        except Exception:  # noqa: BLE001 — a projection outage costs a cache miss, never a lost span
            logger.exception("[spans] export failed; the fact is already on the WAL")


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
        """Mint a span. Passing the parent's ``trace_id``/``span_id`` nests —
        that threading is what turns isolated records into one tree per run."""
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
        # Deterministic on the trace id: every span of a trace samples in or
        # out together (no half-traced trees), and a replay decides the same.
        if self._sample_rate >= 1.0:
            return True
        if self._sample_rate <= 0.0:
            return False
        # Hash the trace id into [0,1) and compare — a pseudo-random but
        # stable coin flip per trace, no RNG state to persist.
        bucket = int(trace_id[:8], 16) % 10_000 / 10_000
        return bucket < self._sample_rate


async def rebuild_spans(
    event_log: EventLog, exporter: SpanExporter, run_id: str
) -> int:
    """The projection criterion, executable: rebuild a run's span cache from
    its span facts. Returns how many spans were re-exported — delete the
    exporter's output, run this, and the cache is back."""
    count = 0
    for event in await event_log.get_events(spans_stream(run_id)):
        if event.event_type != SpanEventType.SPAN_RECORDED:
            continue
        await exporter.export(AgentSpan.from_dict(event.data["span"]))
        count += 1
    return count
