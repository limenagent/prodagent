"""SESSION_END drain: handlers registered as bound methods get their flush() called."""

from __future__ import annotations

import asyncio
from typing import Any

from prodagent import Agent, AgentConfig, RunState
from prodagent.kernel.bus import HookEvent, HookRegistry
from prodagent.llm.fake import script


class _DrainProbe:
    def __init__(self) -> None:
        self.flushed = False

    async def flush(self) -> None:
        self.flushed = True

    async def _on_session_end(self, **_: Any) -> None:
        return


def test_session_end_drains_flush_owner() -> None:
    probe = _DrainProbe()
    hooks = HookRegistry()
    hooks.register_event(HookEvent.SESSION_END, probe._on_session_end)
    agent = Agent(
        name="flush-probe",
        config=AgentConfig(
            name="flush-probe",
            llm=script({"content": "ok"}),
            hooks=hooks,
        ),
    )
    run = asyncio.run(agent.chat("hi", session_id="flush-1"))
    assert run.state == RunState.COMPLETED
    assert probe.flushed, "RunLoop must drain flush() on the handler's instance"
