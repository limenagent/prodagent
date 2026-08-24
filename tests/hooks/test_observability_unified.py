import json
from pathlib import Path

from prodagent.backends.file.span import FileSpanExporter
from prodagent.hooks.audit import AgentSpan, AuditLogger
from prodagent.hooks.bundles.observability import SpanObserverHooks
from prodagent.kernel.bus import HookEvent, HookRegistry


async def test_file_exporter_writes_span_as_jsonl(tmp_path: Path):
    path = tmp_path / "trace.jsonl"
    exporter = FileSpanExporter(path)
    span = AgentSpan(
        span_id="abc123",
        trace_id="trace1",
        run_id="run1",
        action="tool.ping",
        input_payload={"host": "localhost"},
        timestamp=1234567890.0,
    )
    await exporter.export(span)
    await exporter.shutdown()

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["span_id"] == "abc123"
    assert record["action"] == "tool.ping"
    assert record["input_payload"] == {"host": "localhost"}


async def test_file_exporter_ignores_sampling(tmp_path: Path):
    path = tmp_path / "trace.jsonl"
    audit = AuditLogger(
        exporter=FileSpanExporter(path),
        sample_rate=0.0,
        force_log_unsampled=True,
    )
    span = AgentSpan(
        span_id="s1",
        trace_id="t1",
        run_id="r1",
        action="llm.think",
        input_payload={},
        timestamp=0.0,
        sampled=False,
    )
    await audit.record(span)
    await audit.shutdown()

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["action"] == "llm.think"


async def test_file_exporter_flushes_after_each_write(tmp_path: Path):
    path = tmp_path / "trace.jsonl"
    exporter = FileSpanExporter(path)
    span = AgentSpan(
        span_id="s1",
        trace_id="t1",
        run_id="r1",
        action="x",
        input_payload={},
        timestamp=0.0,
    )
    await exporter.export(span)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    await exporter.shutdown()


async def test_span_observer_emits_span_for_instant_events(tmp_path: Path):
    path = tmp_path / "trace.jsonl"
    audit = AuditLogger(exporter=FileSpanExporter(path))
    hooks = HookRegistry()
    SpanObserverHooks(audit=audit).attach(hooks)

    await hooks.fire(
        HookEvent.SESSION_START,
        run_id="run-x",
        task="test task",
        phases=1,
    )
    await hooks.fire(
        HookEvent.LLM_REQUEST, run_id="run-x", system="You are helpful.", messages=[], msg_count=0
    )
    await hooks.fire(HookEvent.THINK, run_id="run-x", text="I should call the ping tool")
    await hooks.fire(HookEvent.MEMORY_RECALL, run_id="run-x", query="ping history", hits=3)
    await hooks.fire(HookEvent.TOKEN_UPDATE, run_id="run-x", input=10, output=5, total=15)
    await hooks.fire(HookEvent.SESSION_END)
    await audit.shutdown()

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").strip().splitlines()]
    actions = [r["action"] for r in records]

    assert "session_start" in actions
    assert "llm.request" in actions
    assert "llm.think" in actions
    assert "memory.recall" in actions
    assert "budget.token_update" in actions

    think_span = next(r for r in records if r["action"] == "llm.think")
    assert think_span["llm_reasoning"] == "I should call the ping tool"
    llm_req_span = next(r for r in records if r["action"] == "llm.request")
    assert len(llm_req_span["system_prompt_version"]) == 8
    mem_span = next(r for r in records if r["action"] == "memory.recall")
    assert mem_span["retrieved_context"] == ["ping history"]


async def test_span_observer_instant_spans_have_zero_latency(tmp_path: Path):
    path = tmp_path / "trace.jsonl"
    audit = AuditLogger(exporter=FileSpanExporter(path))
    hooks = HookRegistry()
    SpanObserverHooks(audit=audit).attach(hooks)

    await hooks.fire(HookEvent.SESSION_START, run_id="r1", task="t", phases=1)
    await hooks.fire(HookEvent.SKILLS_READY, run_id="r1", count=3)
    await hooks.fire(HookEvent.SESSION_END, run_id="r1")
    await audit.shutdown()

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").strip().splitlines()]
    skills_span = next(r for r in records if r["action"] == "skills.ready")
    assert skills_span["latency_ms"] == 0.0


async def test_span_observer_paired_events_still_have_latency(tmp_path: Path):
    path = tmp_path / "trace.jsonl"
    audit = AuditLogger(exporter=FileSpanExporter(path))
    hooks = HookRegistry()
    SpanObserverHooks(audit=audit).attach(hooks)

    await hooks.fire(HookEvent.SESSION_START, run_id="r1", task="t", phases=1)
    await hooks.fire(
        HookEvent.TOOL_CALL, call_id="c1", run_id="r1", name="ping", params={"host": "localhost"}
    )
    await hooks.fire(
        HookEvent.TOOL_RESULT,
        call_id="c1",
        run_id="r1",
        name="ping",
        result={"alive": True},
        elapsed_ms=42.0,
    )
    await hooks.fire(HookEvent.SESSION_END, run_id="r1")
    await audit.shutdown()

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").strip().splitlines()]
    tool_span = next(r for r in records if r["action"] == "ping")
    assert tool_span["latency_ms"] == 42.0
    assert tool_span["output"] == {"alive": True}


async def test_span_observer_parallel_same_name_spans_are_independent(tmp_path: Path):
    path = tmp_path / "trace.jsonl"
    audit = AuditLogger(exporter=FileSpanExporter(path))
    hooks = HookRegistry()
    SpanObserverHooks(audit=audit).attach(hooks)

    await hooks.fire(HookEvent.SESSION_START, run_id="r1", task="t", phases=1)
    await hooks.fire(HookEvent.TOOL_CALL, call_id="call-a", name="ping", params={"host": "a"})
    await hooks.fire(HookEvent.TOOL_CALL, call_id="call-b", name="ping", params={"host": "b"})
    await hooks.fire(
        HookEvent.TOOL_RESULT,
        call_id="call-a",
        name="ping",
        result={"alive": True, "host": "a"},
        elapsed_ms=10.0,
    )
    await hooks.fire(
        HookEvent.TOOL_RESULT,
        call_id="call-b",
        name="ping",
        result={"alive": True, "host": "b"},
        elapsed_ms=20.0,
    )
    await hooks.fire(HookEvent.SESSION_END)
    await audit.shutdown()

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").strip().splitlines()]
    ping_spans = [r for r in records if r["action"] == "ping"]
    assert len(ping_spans) == 2
    outputs = sorted(s["output"]["host"] for s in ping_spans)
    assert outputs == ["a", "b"]
    latencies = sorted(s["latency_ms"] for s in ping_spans)
    assert latencies == [10.0, 20.0]
