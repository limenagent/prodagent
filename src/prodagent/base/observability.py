"""AgentSpan — the decision-snapshot value type shared by ports, backends, and resilience."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


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
        """Compact single-line form for file/console sinks — the fields an
        operator scans for, not the full payload."""
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
