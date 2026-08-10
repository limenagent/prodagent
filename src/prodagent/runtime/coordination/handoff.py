"""Handoff packet, contract, and interceptor for inter-agent delegation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from prodagent.core.exceptions import ContractViolationError

_DEFAULT_PRIOR_OUTPUT_MAX_CHARS = 2000


@dataclass
class HandoffPacket:
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_description: str = ""
    constraints: list[str] = field(default_factory=list)
    available_tools: list[str] = field(default_factory=list)
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    input_refs: dict[str, str] = field(default_factory=dict)
    prior_output: str = ""
    prior_output_max_chars: int = _DEFAULT_PRIOR_OUTPUT_MAX_CHARS

    def to_task_prompt(self) -> str:
        lines = [self.task_description.strip(), ""]
        if self.prior_output:
            trimmed = self.prior_output[: self.prior_output_max_chars]
            if len(self.prior_output) > self.prior_output_max_chars:
                trimmed += f"\n…(truncated, {len(self.prior_output) - self.prior_output_max_chars} more chars)"
            lines.append("Prior agent output:")
            lines.append(trimmed)
            lines.append("")
        if self.constraints:
            lines.append("Constraints:")
            lines.extend(f"  - {c}" for c in self.constraints)
        if self.available_tools:
            lines.append("Available tools:")
            lines.append("  - " + "\n  - ".join(self.available_tools))
        if self.input_refs:
            lines.append("Input references (resolve via tools, do not inline):")
            lines.extend(f"  - {name}: {handle}" for name, handle in self.input_refs.items())
        return "\n".join(lines)


@dataclass
class HandoffContract:
    required_fields: list[str]
    field_types: dict[str, type] = field(default_factory=dict)
    optional_fields: list[str] = field(default_factory=list)
    strict: bool = True

    def validate(self, result: dict[str, Any]) -> tuple[bool, str | None]:
        for name in self.required_fields:
            if name not in result:
                return False, f"missing required field {name!r}"
            expected = self.field_types.get(name)
            if expected is not None and not isinstance(result[name], expected):
                return (
                    False,
                    f"field {name!r} expected {expected.__name__}, got {type(result[name]).__name__}",
                )
        for name in self.optional_fields:
            if name in result:
                expected = self.field_types.get(name)
                if expected is not None and not isinstance(result[name], expected):
                    return (
                        False,
                        f"field {name!r} expected {expected.__name__}, got {type(result[name]).__name__}",
                    )
        return True, None


class HandoffInterceptor:
    """Welded into ``Spawn.spawn`` — runs on every child result."""

    def intercept(self, result: dict[str, Any], contract: HandoffContract) -> dict[str, Any]:
        allowed = set(contract.required_fields) | set(contract.optional_fields)
        filtered = {k: v for k, v in result.items() if k in allowed}

        ok, error = contract.validate(filtered)
        if not ok:
            raise ContractViolationError(
                f"SubAgent response violates the contract: {error}", field=error or ""
            )
        return filtered
