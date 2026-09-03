from __future__ import annotations

from prodagent.kernel.run import Run
from prodagent.kernel.types import RunState
from prodagent.ports.persistence import conversation_messages


def _reactive_run() -> Run:
    run = Run(run_id="r1", task="fix the bug")
    run.state = RunState.COMPLETED
    run.messages = [
        {"role": "user", "content": "fix the bug"},
        {
            "role": "assistant",
            "content": (
                "I will investigate root cause by checking logs and metrics from the last hour "
                "to identify any anomalies in the payment-service."
            ),
        },
        {"role": "assistant", "content": "short"},
    ]
    return run


def _plan_first_run() -> Run:
    run = Run(run_id="r2", task="fix the bug")
    run.state = RunState.COMPLETED
    run.messages = [
        {"role": "user", "content": "fix the bug"},
        {
            "role": "assistant",
            "content": (
                "Plan: investigate the OOM kill on payment-service, then roll back to the "
                "prior good SHA and verify SLO recovery."
            ),
        },
        {
            "role": "assistant",
            "content": (
                "Turn investigate: Root cause identified — OOM kill from unbounded heap "
                "growth in payment-service v2.15.0 introduced by PR #4412 which removed "
                "the buffer pool pattern."
            ),
        },
        {
            "role": "assistant",
            "content": (
                "Turn remediate: Rolled back payment-service to v2.14.0 (SHA f8c01d4). "
                "SLO recovered to 99.97% within 4 minutes post-rollback. Incident "
                "INC-20260619-001 resolved."
            ),
        },
    ]
    return run


def test_conversation_messages_reactive_returns_copy_of_messages():
    run = _reactive_run()
    msgs = conversation_messages(run)
    assert msgs == [dict(m) for m in run.messages]


def test_conversation_messages_reactive_is_a_copy_not_same_object():
    run = _reactive_run()
    msgs = conversation_messages(run)
    msgs[0]["content"] = "mutated"
    assert run.messages[0]["content"] == "fix the bug"


def test_conversation_messages_plan_first_starts_with_user_task():
    run = _plan_first_run()
    msgs = conversation_messages(run)
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == run.task


def test_conversation_messages_plan_first_includes_step_actions():
    run = _plan_first_run()
    msgs = conversation_messages(run)
    contents = " ".join(m.get("content", "") for m in msgs)
    assert "investigate" in contents
    assert "remediate" in contents


def test_conversation_messages_plan_first_all_roles_are_user_or_assistant():
    run = _plan_first_run()
    msgs = conversation_messages(run)
    for m in msgs:
        assert m["role"] in ("user", "assistant")


def test_conversation_messages_empty_run_returns_user_task_only():
    run = Run(run_id="empty", task="do something")
    msgs = conversation_messages(run)
    assert len(msgs) == 1
    assert msgs[0] == {"role": "user", "content": "do something"}
