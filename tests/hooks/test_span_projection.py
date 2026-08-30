"""Span-projection laws: the exporter's output is a rebuildable cache.

Law 1 (the projection criterion, executable): a run's spans are facts on
``<run_id>#spans``; delete the exporter's output entirely, rebuild from the
WAL, and the cache comes back equivalent (same spans, same order).

Law 2: the fact lands even when the exporter is absent or fails — the span
observer never loses a span to a projection outage.

Law 3: the run-scope route — a driver that opens ``run_scope`` with its
event log makes any AuditLogger on the task WAL-recording without holding a
store of its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from prodagent.backends.file.span import FileSpanExporter
from prodagent.backends.memory.event_log import InMemoryEventLog
from prodagent.base.event_log import SpanEventType, spans_stream
from prodagent.base.observability import AgentSpan
from prodagent.base.run_context import run_scope
from prodagent.hooks.audit import AuditLogger, rebuild_spans
from prodagent.kernel.bus import HookRegistry
from prodagent.kernel.loop import ReactiveLoop
from prodagent.kernel.types import SideEffectLevel, ToolMeta
from prodagent.llm.fake import script
from prodagent.tooling.base import FunctionTool
from prodagent.tooling.dispatcher import ToolDispatcher


class _CollectingExporter:
    """In-memory projection cache — deletable by fiat, inspectable by the law."""

    def __init__(self) -> None:
        self.spans: list[AgentSpan] = []

    async def export(self, span: AgentSpan) -> None:
        self.spans.append(span)

    async def shutdown(self) -> None: ...


def _tool(name: str) -> FunctionTool:
    async def fn(**_: Any) -> dict:
        return {"action": name}

    return FunctionTool(
        name=name,
        fn=fn,
        meta=ToolMeta(name=name, is_readonly=True, side_effect_level=SideEffectLevel.LOW),
        schema={"name": name, "description": name, "parameters": {"type": "object", "properties": {}}},
    )


def _audit_span(run_id: str, action: str) -> AgentSpan:
    return AgentSpan(span_id=f"s-{action}", trace_id="t", run_id=run_id, action=action,
                     input_payload={}, timestamp=1.0)


async def _drive_run(hooks: HookRegistry, log: InMemoryEventLog) -> str:
    dispatcher = ToolDispatcher({"probe": _tool("probe")}, event_log=log)
    loop = ReactiveLoop(script({"content": "done"}), dispatcher, event_log=log, hooks=hooks)
    run_id: str | None = None
    async for event in loop.stream("task"):
        run_id = getattr(event, "run_id", None) or run_id
    assert run_id is not None
    return run_id


async def test_projection_criterion_delete_and_rebuild(tmp_path: Path) -> None:
    """The law: wipe the exporter's output, replay the WAL, cache restored."""
    log = InMemoryEventLog()
    exporter = FileSpanExporter(tmp_path / "spans.jsonl")
    audit = AuditLogger(exporter=exporter, event_log=log)
    run_id = "r-proj"
    for action in ("agent.run", "tool.call", "tool.result"):
        await audit.record(_audit_span(run_id, action))

    cache_file = tmp_path / "spans.jsonl"
    original = cache_file.read_text(encoding="utf-8")
    facts = await log.get_events(spans_stream(run_id))
    assert len(facts) == 3, "facts landed on the WAL"
    assert all(e.event_type == SpanEventType.SPAN_RECORDED for e in facts)

    cache_file.unlink()  # the projection dies
    rebuilt = await rebuild_spans(log, FileSpanExporter(cache_file), run_id)
    assert rebuilt == 3
    assert cache_file.read_text(encoding="utf-8") == original, "rebuild is equivalent"


async def test_fact_survives_exporter_outage() -> None:
    class FailingExporter:
        async def export(self, span: AgentSpan) -> None:
            raise RuntimeError("projection store down")

        async def shutdown(self) -> None: ...

    log = InMemoryEventLog()
    audit = AuditLogger(exporter=FailingExporter(), event_log=log)
    await audit.record(_audit_span("r-out", "agent.run"))  # must not raise
    assert len(await log.get_events(spans_stream("r-out"))) == 1, "fact landed anyway"


async def test_run_scope_route_makes_observers_wal_recording() -> None:
    """A driver opening run_scope with its log: an AuditLogger constructed
    without any store picks the WAL up from the task's context."""
    log = InMemoryEventLog()
    exporter = _CollectingExporter()
    audit = AuditLogger(exporter=exporter)  # no explicit event_log
    with run_scope("r-ctx", log):
        await audit.record(_audit_span("r-ctx", "tool.call"))
    assert len(await log.get_events(spans_stream("r-ctx"))) == 1
    assert len(exporter.spans) == 1


async def test_live_run_spans_are_facts(tmp_path: Path) -> None:
    """End to end: a REACTIVE run with the observer attached records its
    spans as facts on the run's spans stream (via the run scope)."""
    from prodagent.hooks.bundles.observability import SpanObserverHooks

    log = InMemoryEventLog()
    hooks = HookRegistry()
    SpanObserverHooks(audit=AuditLogger(exporter=_CollectingExporter())).attach(hooks)
    run_id = await _drive_run(hooks, log)

    facts = await log.get_events(spans_stream(run_id))
    assert facts, "the run's spans landed on the WAL"
    # Loop-span bookkeeping rides the bus inside the scope — at minimum the
    # run bracket and tool spans must be facts.
    actions = {e.data["span"]["action"] for e in facts}
    assert "agent.run" in actions
