from __future__ import annotations

import sys
import time
from contextlib import contextmanager

import pytest

from prodagent.resilience.observability.audit import AgentSpan


def _make_span(**overrides) -> AgentSpan:
    defaults = {
        "span_id": "0123456789abcdef",
        "trace_id": "fedcba9876543210" + "0" * 16,
        "run_id": "run-001",
        "action": "llm.complete",
        "input_payload": {"prompt": "hello"},
        "timestamp": time.time(),
        "parent_span_id": None,
        "input_tokens": 120,
        "output_tokens": 45,
        "cost_usd": 0.0021,
        "system_prompt_version": "v3",
        "retrieved_context": ["ctx-a", "ctx-b"],
        "llm_reasoning": "thinking…",
        "error": None,
        "sampled": True,
    }
    defaults.update(overrides)
    return AgentSpan(**defaults)


otel = pytest.importorskip("opentelemetry.sdk.trace", reason="opentelemetry-sdk not installed")


async def test_exporter_constructs_with_console_protocol():
    from prodagent.resilience.observability.otel_exporter import OtelSpanExporter

    exporter = OtelSpanExporter(protocol="console", service_name="test-svc")
    assert exporter is not None
    await exporter.shutdown()


def test_exporter_rejects_unknown_protocol():
    from prodagent.resilience.observability.otel_exporter import OtelSpanExporter

    with pytest.raises(ValueError, match="Unknown OTel protocol"):
        OtelSpanExporter(protocol="carrier-pigeon")


async def test_export_creates_span_with_genai_attributes():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        SimpleSpanProcessor,
        in_memory_span_exporter,
    )

    from prodagent.resilience.observability.otel_exporter import OtelSpanExporter

    exporter = OtelSpanExporter(protocol="console", service_name="test-svc")
    memory = in_memory_span_exporter.InMemorySpanExporter()
    exporter._provider.shutdown()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    exporter._provider = provider
    exporter._tracer = provider.get_tracer("prodagent", "0.1.0")

    span = _make_span()
    await exporter.export(span)

    finished = memory.get_finished_spans()
    assert len(finished) == 1
    emitted = finished[0]
    assert emitted.name == "llm.complete"
    attrs = dict(emitted.attributes)
    assert attrs["gen_ai.usage.input_tokens"] == 120
    assert attrs["gen_ai.usage.output_tokens"] == 45
    assert attrs["prodagent.run_id"] == "run-001"
    assert attrs["prodagent.cost_usd"] == pytest.approx(0.0021, abs=1e-6)
    assert attrs["prodagent.system_prompt_version"] == "v3"
    assert attrs["prodagent.retrieved_context_count"] == 2
    assert attrs["prodagent.llm_reasoning"] == "thinking…"
    await exporter.shutdown()


async def test_export_marks_error_status_and_records_exception():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        SimpleSpanProcessor,
        in_memory_span_exporter,
    )
    from opentelemetry.trace import StatusCode

    from prodagent.resilience.observability.otel_exporter import OtelSpanExporter

    exporter = OtelSpanExporter(protocol="console")
    memory = in_memory_span_exporter.InMemorySpanExporter()
    exporter._provider.shutdown()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    exporter._provider = provider
    exporter._tracer = provider.get_tracer("prodagent", "0.1.0")

    span = _make_span(error="model timeout")
    await exporter.export(span)

    emitted = memory.get_finished_spans()[0]
    assert emitted.status.status_code == StatusCode.ERROR
    assert "model timeout" in (emitted.status.description or "")
    assert len(emitted.events) >= 1
    await exporter.shutdown()


async def test_export_after_shutdown_is_dropped(caplog):
    from prodagent.resilience.observability.otel_exporter import OtelSpanExporter

    exporter = OtelSpanExporter(protocol="console")
    await exporter.shutdown()
    caplog.set_level("WARNING")
    await exporter.export(_make_span())
    assert any("span dropped" in r.message for r in caplog.records)


def test_hex_to_trace_id_pads_short_hex():
    from prodagent.resilience.observability.otel_exporter import _hex_to_trace_id

    assert _hex_to_trace_id("1") == 1
    assert _hex_to_trace_id("not-hex") == 0
    full = "f" * 32
    assert _hex_to_trace_id(full) == int(full, 16)


def test_hex_to_span_id_pads_short_hex():
    from prodagent.resilience.observability.otel_exporter import _hex_to_span_id

    assert _hex_to_span_id("1") == 1
    assert _hex_to_span_id("zzz") == 0
    full = "a" * 16
    assert _hex_to_span_id(full) == int(full, 16)


async def test_otel_satisfies_span_exporter_protocol():
    from prodagent.ports.span import SpanExporter
    from prodagent.resilience.observability.otel_exporter import OtelSpanExporter

    exporter = OtelSpanExporter(protocol="console")
    assert isinstance(exporter, SpanExporter)
    await exporter.shutdown()


class _ImportBlocker:
    def __init__(self, prefixes: tuple[str, ...]) -> None:
        self._prefixes = prefixes

    def _matches(self, name: str) -> bool:
        return any(name == p or name.startswith(p + ".") for p in self._prefixes)

    def find_spec(self, name, path=None, target=None):
        if not self._matches(name):
            return None
        from importlib.util import spec_from_loader

        class _RaisingLoader:
            def create_module(self, spec):  # noqa: ARG002
                return None

            def exec_module(self, module):  # noqa: ARG002
                raise ImportError(f"blocked for test: {name}")

        return spec_from_loader(name, _RaisingLoader())


@contextmanager
def _blocked_imports(*prefixes: str):
    real_modules = {
        name: sys.modules.pop(name)
        for name in list(sys.modules)
        if any(name == p or name.startswith(p + ".") for p in prefixes)
    }
    blocker = _ImportBlocker(prefixes)
    sys.meta_path.insert(0, blocker)
    try:
        yield
    finally:
        sys.meta_path.remove(blocker)
        sys.modules.update(real_modules)


def test_missing_otel_raises_with_install_hint():
    with _blocked_imports("opentelemetry"):
        sys.modules.pop("prodagent.resilience.observability.otel_exporter", None)
        from prodagent.resilience.observability.otel_exporter import OtelSpanExporter

        with pytest.raises(RuntimeError) as excinfo:
            OtelSpanExporter(protocol="console")
        assert "prodagent[otel]" in str(excinfo.value)


async def test_console_protocol_works_without_otlp_exporters():
    with _blocked_imports(
        "opentelemetry.exporter.otlp.proto.grpc",
        "opentelemetry.exporter.otlp.proto.http",
    ):
        from prodagent.resilience.observability.otel_exporter import OtelSpanExporter

        exporter = OtelSpanExporter(protocol="console", service_name="lite-svc")
        await exporter.export(_make_span())
        await exporter.shutdown()


def test_grpc_protocol_fails_clearly_without_grpc_exporter():
    with _blocked_imports("opentelemetry.exporter.otlp.proto.grpc"):
        from prodagent.resilience.observability.otel_exporter import OtelSpanExporter

        with pytest.raises(RuntimeError, match="OTLP gRPC exporter requires"):
            OtelSpanExporter(protocol="grpc", endpoint="http://localhost:4317")
