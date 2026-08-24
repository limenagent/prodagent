"""HandoffPacket — the DOWNSTREAM carrier for task-shaped crossings.

The wire format a delegating side assembles for the side picking the work up:
task description, hard constraints, authorized tools, input handles. It is a
whitelist by construction — there is a field for the task and none for the
sender's conversation history or reasoning, so downstream sanitization is
mostly "assemble the packet correctly" rather than "scrub the context".

Used by ``agents=`` (spawn: parent → child) and ``peers=`` (relay: one agent
→ the next). Carried as the typed payload of a
:class:`~prodagent.coordination.messaging.envelope.Crossing`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from prodagent.core.text import bound_text

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
            trimmed = bound_text(self.prior_output, self.prior_output_max_chars)
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
