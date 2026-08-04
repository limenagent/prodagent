from __future__ import annotations

from prodagent.cognition.memory.classification import reasoning_texts
from prodagent.core.state.run import AgentRun
from prodagent.core.types import RunState


def _reactive_run() -> AgentRun:
    run = AgentRun(run_id="r1", task="fix the bug")
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


def _plan_first_run() -> AgentRun:
    run = AgentRun(run_id="r2", task="fix the bug")
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
                "Step investigate: Root cause identified — OOM kill from unbounded heap "
                "growth in payment-service v2.15.0 introduced by PR #4412 which removed "
                "the buffer pool pattern."
            ),
        },
        {
            "role": "assistant",
            "content": (
                "Step remediate: Rolled back payment-service to v2.14.0 (SHA f8c01d4). "
                "SLO recovered to 99.97% within 4 minutes post-rollback. Incident "
                "INC-20260619-001 resolved."
            ),
        },
    ]
    return run


def test_reasoning_texts_reactive_returns_assistant_messages():
    run = _reactive_run()
    texts = reasoning_texts(run)
    assert len(texts) == 1
    assert "investigate" in texts[0].lower()


def test_reasoning_texts_reactive_filters_short_messages():
    run = _reactive_run()
    texts = reasoning_texts(run)
    for t in texts:
        assert len(t) > 80


def test_reasoning_texts_plan_first_returns_step_messages():
    run = _plan_first_run()
    texts = reasoning_texts(run)
    assert len(texts) == 3
    combined = " ".join(texts)
    assert "OOM" in combined
    assert "Rolled back" in combined


def test_reasoning_texts_filters_short_outputs():
    run = AgentRun(run_id="r3", task="t")
    run.messages = [
        {"role": "user", "content": "t"},
        {"role": "assistant", "content": "short"},
        {"role": "assistant", "content": "long enough output " * 5},
    ]
    texts = reasoning_texts(run)
    assert len(texts) == 1
    assert "long enough" in texts[0]


def test_reasoning_texts_empty_run_returns_empty_list():
    run = AgentRun(run_id="empty", task="t")
    assert reasoning_texts(run) == []
