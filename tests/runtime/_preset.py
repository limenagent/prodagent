"""Shared test helper: JSON step dicts → a validated preset Plan.

The old Planner's static-draft role, kept as a direct constructor for
tests: the framework no longer drafts graphs (column 24 — a model's plan
is task-list data), so graph-mode harnesses hand their plans in as
presets. Step forms mirror the old wire: ``action`` (a tool name),
``unit`` (a registry key). ``goal`` steps are gone with the planner — a
loop is composed in code now.
"""

from __future__ import annotations

import json
from typing import Any

from prodagent.kernel.bodies import ToolBody
from prodagent.kernel.graph import Node, Plan


def preset_plan(steps: str | dict[str, Any]) -> Plan:
    """``{"steps": [{"id", "action", "params", "depends_on", "terminal"}]}``
    (or the step list directly) → validated Plan."""
    if isinstance(steps, str):
        steps = json.loads(steps)
    raw = steps.get("steps", steps) if isinstance(steps, dict) else steps
    return Plan(
        nodes=[
            Node(
                node_id=str(s["id"]),
                body=ToolBody(tool=str(s["action"])),
                params=dict(s.get("params") or {}),
                depends_on=list(s.get("depends_on") or []),
                is_terminal=bool(s.get("terminal")),
            )
            for s in raw
        ]
    )
