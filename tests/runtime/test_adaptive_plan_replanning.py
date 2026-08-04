import pytest

from prodagent.runtime.plan.dag import Plan, PlanStep, StepStatus


def _step(sid: str, depends_on: list[str] | None = None, **kw) -> PlanStep:
    return PlanStep(step_id=sid, action=sid, depends_on=depends_on or [], **kw)


def _completed(sid: str, depends_on: list[str] | None = None) -> PlanStep:
    s = _step(sid, depends_on)
    s.status = StepStatus.COMPLETED
    return s


class TestMergeIntraBatchDependencies:
    def test_chain_of_new_steps_accepted(self):
        plan = Plan()
        plan.add_steps([_step("s1")])
        plan._steps["s1"].status = StepStatus.FAILED

        plan.mark_downstream_obsolete("s1")

        plan.merge(
            [
                _step("s1_prime", replaces_step_id="s1"),
                _step("s2_prime", depends_on=["s1_prime"]),
            ]
        )

        assert plan.get_step("s1_prime").status == StepStatus.PENDING
        assert plan.get_step("s2_prime").status == StepStatus.PENDING

    def test_diamond_of_new_steps_accepted(self):
        plan = Plan()
        plan.add_steps([_step("root")])
        plan._steps["root"].status = StepStatus.FAILED
        plan.mark_downstream_obsolete("root")

        plan.merge(
            [
                _step("root_prime", replaces_step_id="root"),
                _step("left_prime", depends_on=["root_prime"]),
                _step("right_prime", depends_on=["root_prime"]),
                _step("join_prime", depends_on=["left_prime", "right_prime"]),
            ]
        )

        assert plan.get_step("join_prime").status == StepStatus.PENDING

    def test_missing_external_dep_still_raises(self):
        plan = Plan()
        plan.add_steps([_step("s1")])

        with pytest.raises(ValueError, match="not found"):
            plan.merge([_step("new_step", depends_on=["ghost_id"])])


class TestMarkDownstreamObsolete:
    def test_pending_past_completed_is_obsoleted(self):
        plan = Plan()
        plan.add_steps(
            [
                _step("s1"),
                _completed("s2", depends_on=["s1"]),
                _step("s3", depends_on=["s2"]),
            ]
        )
        plan._steps["s1"].status = StepStatus.FAILED

        obsoleted = plan.mark_downstream_obsolete("s1")

        assert plan.get_step("s1").status == StepStatus.FAILED, (
            "Failed step must retain FAILED status, not be overwritten to OBSOLETE"
        )
        assert plan.get_step("s2").status == StepStatus.COMPLETED, (
            "COMPLETED step must not be downgraded to OBSOLETE"
        )
        assert plan.get_step("s3").status == StepStatus.OBSOLETE, (
            "Transitive downstream past a COMPLETED step must be OBSOLETE"
        )

        assert "s1" not in obsoleted
        assert "s2" not in obsoleted
        assert "s3" in obsoleted

    def test_deep_chain_past_completed(self):
        plan = Plan()
        plan.add_steps(
            [
                _step("root"),
                _completed("A", depends_on=["root"]),
                _completed("B", depends_on=["A"]),
                _step("C", depends_on=["B"]),
                _step("D", depends_on=["C"]),
            ]
        )
        plan._steps["root"].status = StepStatus.FAILED

        plan.mark_downstream_obsolete("root")

        assert plan.get_step("A").status == StepStatus.COMPLETED
        assert plan.get_step("B").status == StepStatus.COMPLETED
        assert plan.get_step("C").status == StepStatus.OBSOLETE
        assert plan.get_step("D").status == StepStatus.OBSOLETE

    def test_no_downstream_completed_only(self):
        plan = Plan()
        plan.add_steps(
            [
                _step("s1"),
                _completed("s2", depends_on=["s1"]),
            ]
        )
        plan._steps["s1"].status = StepStatus.FAILED

        obsoleted = plan.mark_downstream_obsolete("s1")

        assert plan.get_step("s2").status == StepStatus.COMPLETED
        assert "s2" not in obsoleted
