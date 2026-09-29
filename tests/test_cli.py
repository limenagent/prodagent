"""Smoke tests for the built-in CLI demo and its trace (the approval-gate flow
that `prodagent run` drives). These guard the playground demo module, which has
no scenario test of its own and once silently drifted onto removed APIs."""

import pytest

from src.kernel.trace import render_trace
from src.playground.demo import build_demo


@pytest.mark.asyncio
async def test_demo_approve_flow():
    wf = build_demo()
    r = await wf.run("Check order O-1234; it is overdue and I'd like a refund.")
    assert str(r.run.state) == "suspended"
    r = await wf.resume(r.run_id, {"approved": True})
    assert str(r.run.state) == "completed"
    assert "Refund executed" in r.output
    text = render_trace(await wf.host().eventlog.all_events())
    assert "support" in text and "refund" in text


@pytest.mark.asyncio
async def test_demo_deny_flow():
    wf = build_demo()
    r = await wf.run("refund O-1234")
    assert str(r.run.state) == "suspended"
    r = await wf.resume(r.run_id, {"approved": False})
    assert str(r.run.state) == "completed"
    assert "denied" in r.output


def test_web_outside_a_checkout_degrades_gracefully(monkeypatch, capsys):
    # On an installed copy the examples package is absent (deliberately not in
    # the wheel); `prodagent web` must say so, not die on a raw ModuleNotFoundError.
    import sys

    import src.cli as cli

    class _BlockExamples:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "examples" or fullname.startswith("examples."):
                raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
            return None

    for name in [n for n in sys.modules if n == "examples" or n.startswith("examples.")] + [
        "src.playground.server",
        "src.playground.scenarios",
    ]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_BlockExamples(), *sys.meta_path])

    assert cli.main(["web"]) == 1
    assert "repository checkout" in capsys.readouterr().out
