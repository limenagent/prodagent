from __future__ import annotations

from prodagent.resilience.observability.drift import DriftDetector


def test_identical_sequences_no_drift() -> None:
    report = DriftDetector().compare(["a", "b", "c"], ["a", "b", "c"])
    assert not report.drifted
    assert report.drifts == []


def test_skipped_actions_detected() -> None:
    report = DriftDetector().compare(["a", "b", "c"], ["a", "c"])
    assert report.drifted
    assert "skipped" in report.kinds
    skipped = [d for d in report.drifts if d.kind == "skipped"]
    assert len(skipped) == 1
    assert "'b'" in skipped[0].detail


def test_extra_actions_detected() -> None:
    report = DriftDetector().compare(["a", "b"], ["a", "b", "x"])
    assert report.drifted
    assert "extra" in report.kinds
    extra = [d for d in report.drifts if d.kind == "extra"]
    assert len(extra) == 1
    assert "'x'" in extra[0].detail


def test_substituted_actions_detected() -> None:
    report = DriftDetector().compare(["a", "b", "c"], ["a", "X", "c"])
    assert report.drifted
    assert "substituted" in report.kinds
    sub = [d for d in report.drifts if d.kind == "substituted"]
    assert len(sub) == 1
    assert "'b'" in sub[0].detail and "'X'" in sub[0].detail


def test_reorder_detected_when_multiset_matches() -> None:
    report = DriftDetector().compare(["a", "b", "c"], ["c", "a", "b"])
    assert report.drifted
    assert "reordered" in report.kinds


def test_reorder_not_flagged_when_multiset_differs() -> None:
    report = DriftDetector().compare(["a", "b", "c"], ["a", "b", "b"])
    assert "reordered" not in report.kinds
    assert "substituted" in report.kinds


def test_multiple_kinds_at_once() -> None:
    report = DriftDetector().compare(["a", "b", "c"], ["X", "c", "x"])
    kinds = report.kinds
    assert "skipped" in kinds
    assert "extra" in kinds
    assert "substituted" in kinds


def test_empty_sequences_no_drift() -> None:
    report = DriftDetector().compare([], [])
    assert not report.drifted


def test_empty_golden_all_extra() -> None:
    report = DriftDetector().compare([], ["a", "b"])
    assert report.drifted
    assert "extra" in report.kinds
    assert len([d for d in report.drifts if d.kind == "extra"]) == 2


def test_report_truthiness_matches_drifted() -> None:
    assert not DriftDetector().compare(["a"], ["a"])
    assert DriftDetector().compare(["a"], ["b"])


def test_detect_against_real_spans() -> None:
    from prodagent.resilience.observability.audit import AgentSpan

    spans = [
        AgentSpan(
            span_id="1", trace_id="t", run_id="r", action="search", input_payload={}, timestamp=0
        ),
        AgentSpan(
            span_id="2", trace_id="t", run_id="r", action="summarise", input_payload={}, timestamp=1
        ),
    ]
    actual = [s.action for s in spans]
    report = DriftDetector().compare(["search", "summarise", "publish"], actual)
    assert report.drifted
    assert "skipped" in report.kinds
