"""ContextAwareApprovalFormatter — human-readable approval prompts.

(The injection-defense half of this file died with guardrail/injection —
zero adopters; see CHANGELOG. Only the approval formatter survives.)
"""

from __future__ import annotations

from prodagent.hooks.approval import ContextAwareApprovalFormatter
from prodagent.kernel.types import ToolCall


class TestContextAwareApprovalFormatter:
    def test_params_truncated_when_long(self):
        fmt = ContextAwareApprovalFormatter()
        call = ToolCall(name="delete_records", params={"ids": list(range(1000))})
        msg = fmt.format(call)
        assert "APPROVAL REQUIRED" in msg
        assert "delete_records" in msg
        params_line = [line for line in msg.splitlines() if "Parameters" in line][0]
        assert len(params_line) < 300

    def test_diff_shown_for_config_change(self):
        fmt = ContextAwareApprovalFormatter()
        call = ToolCall(name="update_config", params={"file": "nginx.conf"})
        msg = fmt.format(call, old_content="timeout 10s\n", new_content="timeout 1s\n")
        assert "diff" in msg.lower() or "-timeout" in msg or "+timeout" in msg

    def test_production_warning_shown(self):
        fmt = ContextAwareApprovalFormatter()
        call = ToolCall(name="delete_records", params={"ids": [1, 2, 3]})
        msg = fmt.format(call, affected_count=3, environment="production")
        assert "PRODUCTION" in msg or "WARNING" in msg
