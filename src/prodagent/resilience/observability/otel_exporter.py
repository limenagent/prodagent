"""OpenTelemetry exporter — bridge AgentSpan to OTLP collectors."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from prodagent.core.observability import AgentSpan

logger = logging.getLogger(__name__)

_HEX_RADIX = 16
_TRACE_ID_BITS = 128
_SPAN_ID_BITS = 64
_TRACE_ID_HEX_LEN = _TRACE_ID_BITS // 4  # 32
_SPAN_ID_HEX_LEN = _SPAN_ID_BITS // 4  # 16


def _hex_to_trace_id(hex_str: str) -> int:
    """32-char hex → OTel int. 0 on malformed input."""
    padded = hex_str.rjust(_TRACE_ID_HEX_LEN, "0")[:_TRACE_ID_HEX_LEN]
    try:
        return int(padded, _HEX_RADIX)
    except ValueError:
        return 0


def _hex_to_span_id(hex_str: str) -> int:
    """16-char hex → OTel int. 0 on malformed input."""
    padded = hex_str.rjust(_SPAN_ID_HEX_LEN, "0")[:_SPAN_ID_HEX_LEN]
    try:
        return int(padded, _HEX_RADIX)
    except ValueError:
        return 0


_OTEL_IMPORT_ERROR = (
    "OpenTelemetry SDK is required for OtelSpanExporter: "
    'pip install "prodagent[otel]"  (or pip install opentelemetry-sdk '
    "opentelemetry-exporter-otlp)"
)


def _require_otel() -> Any:
    try:
        from opentelemetry import trace as _trace  # noqa: F401
        from opentelemetry.sdk.trace import TracerProvider  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised via missing-dep test
        raise RuntimeError(_OTEL_IMPORT_ERROR) from exc
    import opentelemetry

    return opentelemetry


class OtelSpanExporter:
    """Export AgentSpans to an OTLP collector; opt-in, not resolvable via backends.factory."""

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        service_name: str = "prodagent",
        protocol: str = "grpc",  # "grpc" | "http" | "console"
        resource_attributes: dict[str, str] | None = None,
    ) -> None:
        otel = _require_otel()
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        self._protocol = protocol
        self._closed = False

        resource = Resource.create(
            {
                "service.name": service_name,
                **(resource_attributes or {}),
            }
        )

        otel_exporter: Any
        if protocol == "console":
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter

            otel_exporter = ConsoleSpanExporter()
        elif protocol == "grpc":
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter as GrpcExporter,
                )
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "OTLP gRPC exporter requires opentelemetry-exporter-otlp-proto-grpc: "
                    'pip install "prodagent[otel]"'
                ) from exc
            otel_exporter = GrpcExporter(endpoint=endpoint) if endpoint else GrpcExporter()
        elif protocol == "http":
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter as HttpExporter,
                )
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "OTLP HTTP exporter requires opentelemetry-exporter-otlp-proto-http: "
                    'pip install "prodagent[otel]"'
                ) from exc
            otel_exporter = HttpExporter(endpoint=endpoint) if endpoint else HttpExporter()
        else:
            raise ValueError(f"Unknown OTel protocol: {protocol!r} (use grpc|http|console)")
        self._otel_exporter = otel_exporter

        self._provider = TracerProvider(resource=resource)
        self._provider.add_span_processor(BatchSpanProcessor(self._otel_exporter))
        self._tracer = self._provider.get_tracer("prodagent", "0.1.0")
        self._otel = otel  # keep ref for lazy attribute access

    async def export(self, span: AgentSpan) -> None:
        if self._closed:
            logger.warning("OtelSpanExporter.export called after shutdown — span dropped")
            return

        from opentelemetry.trace import SpanKind, Status, StatusCode

        trace_id = _hex_to_trace_id(span.trace_id)
        parent_span_id = _hex_to_span_id(span.parent_span_id) if span.parent_span_id else None

        otel_span = self._tracer.start_span(
            span.action,
            kind=SpanKind.INTERNAL,
            start_time=int(span.timestamp * 1_000_000_000),
            attributes=self._build_attributes(span),
        )

        if parent_span_id is not None and trace_id != 0:
            self._attach_parent(otel_span, trace_id, parent_span_id)

        if span.error:
            otel_span.set_status(Status(StatusCode.ERROR, span.error))
            otel_span.record_exception(RuntimeError(span.error))
        else:
            otel_span.set_status(Status(StatusCode.OK))

        otel_span.end(end_time=int((span.timestamp + span.latency_ms / 1000.0) * 1_000_000_000))

    async def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._provider.force_flush()
            self._provider.shutdown()
        except Exception as exc:  # pragma: no cover - depends on backend
            logger.error("OtelSpanExporter shutdown failed: %s", exc)

    @staticmethod
    def _build_attributes(span: AgentSpan) -> dict[str, Any]:
        """Map AgentSpan fields to GenAI semantic conventions + prodagent extensions."""
        attrs: dict[str, Any] = {
            "gen_ai.usage.input_tokens": span.input_tokens,
            "gen_ai.usage.output_tokens": span.output_tokens,
            "prodagent.run_id": span.run_id,
            "prodagent.cost_usd": round(span.cost_usd, 6),
            "prodagent.sampled": span.sampled,
        }
        if span.system_prompt_version:
            attrs["prodagent.system_prompt_version"] = span.system_prompt_version
        if span.retrieved_context:
            attrs["prodagent.retrieved_context_count"] = len(span.retrieved_context)
        if span.llm_reasoning:
            # Truncate to stay under OTel's 65 KiB per-attribute limit.
            attrs["prodagent.llm_reasoning"] = span.llm_reasoning[:4096]
        return attrs

    def _attach_parent(self, otel_span: Any, trace_id: int, parent_span_id: int) -> None:

        from opentelemetry.trace import SpanContext, TraceFlags
        from opentelemetry.trace.span import TraceState

        ctx = SpanContext(
            trace_id=trace_id,
            span_id=parent_span_id,
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
            trace_state=TraceState(),
        )
        try:
            otel_span._context = ctx
        except (AttributeError, TypeError):
            # Fall back silently — export an orphan span rather than crash the audit path.
            logger.debug("Could not attach parent context to OTel span", exc_info=True)


__all__ = ["OtelSpanExporter"]
