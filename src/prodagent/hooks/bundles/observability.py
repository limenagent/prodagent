"""Observability hook bundles."""

from __future__ import annotations

import hashlib
import logging
import time
from typing import TYPE_CHECKING, Any

from prodagent.hooks.events import HookEvent

if TYPE_CHECKING:
    from prodagent.core.config import FrameworkConfig
    from prodagent.core.observability import AgentSpan
    from prodagent.hooks.registry import HookRegistry
    from prodagent.ports.span import SpanExporter
    from prodagent.resilience.observability.audit import AuditLogger

logger = logging.getLogger(__name__)


class SpanObserverHooks:
    def __init__(
        self,
        *,
        audit: AuditLogger | None = None,
        exporter: SpanExporter | None = None,
        framework_config: FrameworkConfig | None = None,
    ) -> None:
        if audit is None:
            from prodagent.resilience.observability.audit import AuditLogger

            if exporter is None and framework_config is not None:
                from prodagent.backends.factory import resolve_span_exporter

                exporter = resolve_span_exporter(framework_config)
            audit = AuditLogger(exporter=exporter)
        self._audit = audit
        self._pending: dict[str, tuple[AgentSpan, float]] = {}
        self._run_spans: dict[str, tuple[AgentSpan, float]] = {}
        self._run_traces: dict[str, str] = {}

    def attach(self, hooks: HookRegistry) -> None:
        from prodagent.hooks.events import HookEvent

        dedicated = {
            HookEvent.SESSION_START: self.on_session_start,
            HookEvent.LOOP_START: self.on_loop_start,
            HookEvent.LOOP_END: self.on_loop_end,
            HookEvent.TOOL_CALL: self.on_tool_call,
            HookEvent.TOOL_RESULT: self.on_tool_result,
            HookEvent.AGENT_SPAWN: self.on_agent_spawn,
            HookEvent.SESSION_END: self.on_session_end,
        }

        for event in HookEvent:
            handler = dedicated.get(event)
            if handler is not None:
                hooks.register_event(event, handler)
            else:
                hooks.register_event(event, self.on_instant)

    def on_session_start(self, *, run_id: str = "", task: str = "", **_: Any) -> None:
        span = self._audit.span(run_id, "session_start", {"task": task[:120]}, root=True)
        self._audit.record(span)
        self._run_traces[run_id] = span.trace_id
        logger.debug("AgentSpan root: trace_id=%s run_id=%s", span.trace_id[:8], run_id[:8])

    def on_loop_start(self, *, run_id: str = "", task: str = "", **_: Any) -> None:
        if not run_id:
            return
        trace_id = self._run_traces.get(run_id)
        span = self._audit.span(run_id, "agent.run", {"task": task[:200]}, trace_id=trace_id)
        self._run_spans[run_id] = (span, time.time())

    def on_loop_end(self, *, run_id: str = "", error: str | None = None, **_: Any) -> None:
        if not run_id:
            return
        entry = self._run_spans.pop(run_id, None)
        if entry is None:
            return
        span, start = entry
        span.latency_ms = (time.time() - start) * 1000
        span.error = error
        self._audit.record(span)

    def on_tool_call(
        self,
        *,
        call_id: str = "",
        run_id: str = "",
        name: str = "",
        params: dict[str, Any] | None = None,
        **_: Any,
    ) -> None:
        if not call_id:
            return
        params = params or {}
        span = self._audit.span(run_id, name, params, trace_id=self._run_traces.get(run_id))
        self._pending[call_id] = (span, time.time())

    def on_tool_result(
        self,
        *,
        call_id: str = "",
        name: str = "",
        result: dict[str, Any] | None = None,
        elapsed_ms: float = 0,
        **_: Any,
    ) -> None:
        if not call_id:
            return
        entry = self._pending.pop(call_id, None)
        if entry is None:
            return
        span, start = entry
        span.output = result or {}
        span.latency_ms = elapsed_ms or (time.time() - start) * 1000
        self._audit.record(span)
        logger.debug(
            "AgentSpan: span_id=%s  action=%s  latency=%.1fms",
            span.span_id[:8],
            name,
            span.latency_ms,
        )

    def on_agent_spawn(self, *, name: str = "", task: str = "", run_id: str = "", **_: Any) -> None:
        span = self._audit.span(
            run_id, f"spawn:{name}", {"task": task[:120]}, trace_id=self._run_traces.get(run_id)
        )
        self._audit.record(span)

    def on_instant(self, *, event_name: str = "", run_id: str = "", **data: Any) -> None:
        if not run_id:
            return
        payload = {k: v for k, v in data.items() if k != "event_name"}
        span = self._audit.span(run_id, event_name, payload, trace_id=self._run_traces.get(run_id))
        self._enrich_from_event(span, event_name, data)
        span.latency_ms = 0.0
        self._audit.record(span)

    def _enrich_from_event(self, span: AgentSpan, event_name: str, data: dict[str, Any]) -> None:
        if event_name == HookEvent.LLM_REQUEST:
            sys_str = data.get("system")
            if sys_str:
                span.system_prompt_version = hashlib.sha256(
                    str(sys_str).encode("utf-8")
                ).hexdigest()[:8]
        elif event_name == HookEvent.THINK:
            text = data.get("text")
            if text:
                span.llm_reasoning = str(text)[:500]
        elif event_name == HookEvent.MEMORY_RECALL:
            q = data.get("query")
            if q:
                span.retrieved_context = [str(q)]
        elif event_name == HookEvent.CONTEXT_BUILD:
            msgs = data.get("messages")
            if isinstance(msgs, list):
                span.retrieved_context = [
                    str(m.get("content", ""))[:200] for m in msgs[:4] if isinstance(m, dict)
                ]

    def on_session_end(self, *, run_id: str = "", run: Any = None, **_: Any) -> None:
        from prodagent.core.state.run import is_child_subordinate

        if run is not None and is_child_subordinate(run):
            return
        self._run_traces.pop(run_id, None)
