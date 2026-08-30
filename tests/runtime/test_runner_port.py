"""RunnerPort end-to-end — the three Activation dispatch modes, each driving
real member activations through the port.

Unit 1's completion criterion (arch-review-2026-08-27-atomicity-verdict.md):
serial / concurrent / single_winner each activate agents via
``RunnerPort.activate`` — the execution-position seam — and a bare spawn-style
activation runs a forked child to terminal state.
"""

from __future__ import annotations

from typing import Any

from prodagent import Agent, ExecutionMode
from prodagent.coordination.infra.stage import StageDriver
from prodagent.kernel.state import RunState, collect_final_run
from prodagent.kernel.types import LLMResponse, RunCompletedEvent
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.ports.execution import Activation, AgentActivation, InProcessChatRunner
from prodagent.runtime.config import AgentConfig
from prodagent.runtime.parent_runtime import ParentRuntime
from prodagent.runtime.runner import InProcessRunner


def _member(name: str, *, text: str) -> Agent:
    return Agent(
        name,
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(
            name=name,
            llm=FakeLLMAdapter(responses=[LLMResponse(content=text, stop_reason="end_turn")]),
        ),
    )


class _Driver(StageDriver[str]):
    """Minimal concrete driver — only ``_dispatch`` is exercised here."""

    async def _rounds(self) -> Any:
        return
        yield  # pragma: no cover — stub async-generator shape

    def _completed(self, reason: Any) -> str:
        return f"done:{reason.reason}"


async def _activate_chat(runner: InProcessChatRunner, agent: Agent) -> str | None:
    """One member activation through the port; returns the terminal output."""
    async for event in runner.activate(
        AgentActivation(agent=agent, task="say your line", session_id=f"s-{agent.name}")
    ):
        if isinstance(event, RunCompletedEvent):
            return event.run.final_output
    return None


async def test_serial_dispatch_activates_each_member_through_port() -> None:
    runner = InProcessChatRunner()
    members = {"a": _member("a", text="alpha"), "b": _member("b", text="beta")}
    spoke: list[str] = []

    async def run_one(name: str) -> str | None:
        out = await _activate_chat(runner, members[name])
        spoke.append(name)
        return out

    driver = _Driver()
    results = await driver._dispatch(
        Activation(members=["a", "b"], dispatch="serial", label="round_robin"), run_one
    )

    assert [(m, out) for m, out in results] == [("a", "alpha"), ("b", "beta")]
    assert spoke == ["a", "b"], "serial dispatch must run members one at a time, in order"


async def test_concurrent_dispatch_activates_members_through_port() -> None:
    runner = InProcessChatRunner()
    members = {
        "slow": _member("slow", text="from slow"),
        "fast": _member("fast", text="from fast"),
    }

    async def run_one(name: str) -> str | None:
        return await _activate_chat(runner, members[name])

    driver = _Driver()
    results = await driver._dispatch(
        Activation(members=["slow", "fast"], dispatch="concurrent", label="free_for_all"),
        run_one,
    )

    # Completion order may interleave; the collected batch stays member-ordered.
    assert dict(results) == {"slow": "from slow", "fast": "from fast"}


async def test_single_winner_dispatch_activates_one_member_through_port() -> None:
    from prodagent.backends.factory import in_process_lock_store

    runner = InProcessChatRunner()
    members = {n: _member(n, text=f"won by {n}") for n in ("w1", "w2")}
    activated: list[str] = []

    async def run_one(name: str) -> str | None:
        out = await _activate_chat(runner, members[name])
        activated.append(name)
        return out

    driver = _Driver()
    results = await driver._dispatch(
        Activation(members=["w1", "w2"], dispatch="single_winner", label="buzz_in"),
        run_one,
        lock_store=in_process_lock_store(),
        lock_scope="runner-port-test",
    )

    winners = [out for _, out in results if out is not None]
    assert activated == ["w1"] or activated == ["w2"], "exactly one member computes"
    assert len(activated) == 1
    assert winners == [f"won by {activated[0]}"]
    # The loser's slot is None — it must never have started real work.
    assert [out for _, out in results].count(None) == 1


async def test_bare_activation_runs_forked_child_through_port() -> None:
    """Spawn-shaped: parent_run_id set, bound runner — the child forks under
    the hop wiring and runs to a terminal event through the port."""
    child = _member("child", text="child done")
    runner = InProcessRunner(ParentRuntime(parent_run_id="root", llm=child.config.llm))

    run = await collect_final_run(
        runner.activate(
            AgentActivation(
                agent=child,
                task="do the sub-task",
                run_id="root::child",
                parent_run_id="root",
                depth=1,
            )
        ),
        fallback_run_id="root::child",
        fallback_task="do the sub-task",
    )

    assert run.state is RunState.COMPLETED
    assert run.final_output == "child done"
