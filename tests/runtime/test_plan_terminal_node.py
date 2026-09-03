from __future__ import annotations

from typing import Any

from prodagent.kernel.bodies import ToolBody
from prodagent.kernel.finalize import finalize_run
from prodagent.kernel.graph import Node, Plan
from prodagent.kernel.node_state import NodeRuntimeState
from prodagent.kernel.run import Run
from prodagent.kernel.types import NodeStatus, RunState


def _make_run() -> Run:
    return Run(run_id="test-run", task="test")


def _node(
    node_id: str,
    *,
    action: str = "noop",
    is_terminal: bool = False,
) -> Node:
    return Node(node_id=node_id, body=ToolBody(action), is_terminal=is_terminal)


def _state(
    node_id: str,
    *,
    status: NodeStatus = NodeStatus.COMPLETED,
    output_ref: Any = None,
) -> NodeRuntimeState:
    return NodeRuntimeState(node_id, status=status, output_ref=output_ref)


class TestFinalizeRunTerminalNode:
    def test_terminal_node_output_becomes_final_output(self):
        run = _make_run()
        plan = Plan(plan_id="test-run")
        plan.add_nodes(
            [
                _node("investigate"),
                _node("fix", action="restart_pod", is_terminal=True),
            ]
        )
        run.node_states = {
            "investigate": _state("investigate", output_ref="Diagnosis: bad config map"),
            "fix": _state("fix", output_ref={"restarted": True}),
        }
        finalize_run(run, plan)

        assert run.state is RunState.COMPLETED
        assert run.final_output == str({"restarted": True})

    def test_non_terminal_last_node_does_not_override_terminal(self):
        run = _make_run()
        plan = Plan(plan_id="test-run")
        plan.add_nodes(
            [_node("report", is_terminal=True), _node("notify", action="send_notification")]
        )
        run.node_states = {
            "report": _state("report", output_ref="Incident report: root cause was X"),
            "notify": _state("notify", output_ref={"sent": True}),
        }
        finalize_run(run, plan)

        assert run.final_output == "Incident report: root cause was X"
        assert run.final_output != str({"sent": True})

    def test_no_terminal_node_falls_back_to_last_tool_result(self):
        run = _make_run()
        plan = Plan(plan_id="test-run")
        plan.add_nodes([_node("node_a"), _node("node_b")])
        run.node_states = {
            "node_a": _state("node_a", output_ref={"a": 1}),
            "node_b": _state("node_b", output_ref={"b": 2}),
        }
        finalize_run(run, plan)

        assert run.final_output == str({"b": 2})

    def test_terminal_node_not_completed_falls_back(self):
        run = _make_run()
        plan = Plan(plan_id="test-run")
        plan.add_nodes([_node("node_a"), _node("terminal", is_terminal=True)])
        run.node_states = {
            "node_a": _state("node_a", output_ref={"a": 1}),
            "terminal": _state("terminal", status=NodeStatus.FAILED, output_ref=None),
        }
        finalize_run(run, plan)

        assert run.final_output == str({"a": 1})

    def test_suspended_run_not_clobbered_to_completed(self):
        run = _make_run()
        run.state = RunState.SUSPENDED
        plan = Plan(plan_id="test-run")
        plan.add_nodes([_node("terminal", is_terminal=True)])
        run.node_states = {"terminal": _state("terminal", output_ref="result")}
        finalize_run(run, plan)

        assert run.state is RunState.SUSPENDED
        assert run.final_output == "result"

    def test_terminal_spawn_agent_node_unwraps_child_output(self):
        run = _make_run()
        plan = Plan(plan_id="test-run")
        child_result = {
            "agent": "sar_submitter",
            "state": "completed",
            "output": "✅ SAR 报告已成功提交至监管系统。",
            "turns": 2,
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "tool_history": [],
        }
        plan.add_nodes([_node("submit_sar", action="spawn_agent", is_terminal=True)])
        run.node_states = {"submit_sar": _state("submit_sar", output_ref=child_result)}
        finalize_run(run, plan)

        assert run.final_output == "✅ SAR 报告已成功提交至监管系统。"
        assert "agent" not in run.final_output
        assert "state" not in run.final_output


class TestNodeTerminalSerialization:
    def test_wire_dict_includes_is_terminal(self):
        node = _node("s1", is_terminal=True)
        d = {"is_terminal": node.is_terminal}
        assert d["is_terminal"] is True

    def test_from_state_restores_is_terminal(self):
        plan = Plan(plan_id="p1")
        plan.add_nodes(
            [
                Node(node_id="s1", body=ToolBody("noop"), is_terminal=True),
                Node(node_id="s2", body=ToolBody("noop"), is_terminal=False),
            ]
        )
        states = {
            "s1": _state("s1"),
            "s2": _state("s2"),
        }

        restored, restored_states = Plan.from_state(plan.to_state(states), plan_id="p1")
        assert restored.get_node("s1").is_terminal is True
        assert restored.get_node("s2").is_terminal is False

    def test_from_state_defaults_is_terminal_false_for_legacy_state(self):
        legacy_state = {
            "version": 1,
            "nodes": {
                "s1": {
                    "node_id": "s1",
                    "action": "noop",
                    "params": {},
                    "depends_on": [],
                }
            },
        }
        plan, _ = Plan.from_state(legacy_state, plan_id="p1")
        assert plan.get_node("s1").is_terminal is False
