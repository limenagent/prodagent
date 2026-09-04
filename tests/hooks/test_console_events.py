"""ConsoleObserver renders the failure/handoff events it used to drop."""

from __future__ import annotations

from prodagent.hooks.observers.console import ConsoleObserverHooks
from prodagent.kernel.bus import HookEvent


def test_console_renders_failure_events(capsys) -> None:
    obs = ConsoleObserverHooks()
    obs.on_event(
        event_name=HookEvent.RUN_FAILED.value,
        run_id="root",
        error="budget exhausted",
        turns=2,
        cost_usd=0.01,
    )
    obs.on_event(event_name=HookEvent.CHECKPOINT_FAILED.value, run_id="root", turns=3)
    obs.on_event(event_name=HookEvent.LOOP_END.value, run_id="root", error="bad turn")
    out = capsys.readouterr().out
    assert "FAILED" in out and "budget exhausted" in out
    assert "save failed" in out
    assert "ended with error" in out


def test_console_loop_end_silent_without_error(capsys) -> None:
    obs = ConsoleObserverHooks()
    obs.on_event(event_name=HookEvent.LOOP_END.value, run_id="root", error=None)
    assert capsys.readouterr().out == ""
