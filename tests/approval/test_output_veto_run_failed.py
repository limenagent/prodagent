from __future__ import annotations

import asyncio

import pytest

from prodagent import Agent, AgentConfig, RunState
from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.base.errors import SensitiveContentDetected
from prodagent.kernel.bus import Gate, HookRegistry
from prodagent.llm.fake import script


async def _veto_on_sensitive_output(final_output: str = "", **_: object) -> None:
    if "alice@example.com" in final_output:
        raise SensitiveContentDetected("L5 output scan found sensitive item(s) in final output")


def _veto_agent(store: FileCheckpointStore) -> Agent:
    hooks = HookRegistry()
    hooks.register_checker(Gate.RUN_COMPLETE, _veto_on_sensitive_output, priority=80)
    return Agent(
        name="veto-demo",
        system_prompt="Answer directly.",
        config=AgentConfig(
            name="veto-demo",
            llm=script({"content": "Contact alice@example.com for details."}),
            hooks=hooks,
            checkpoint=store,
        ),
    )


def test_sensitive_content_veto_marks_run_failed(tmp_path):
    """A VETO-disposition SensitiveContentDetected at RUN_COMPLETE fails the run.

    The widened except in RunLoop._settle persists a FAILED checkpoint before
    re-raising — without it the veto would escape as an unhandled error.
    """
    store = FileCheckpointStore(tmp_path)
    agent = _veto_agent(store)

    with pytest.raises(SensitiveContentDetected):
        asyncio.run(agent.chat("give me the contact", session_id="veto-run"))

    persisted = asyncio.run(store.load("veto-run:1"))
    assert persisted is not None
    assert persisted.state == RunState.FAILED


def test_clean_run_unaffected_by_veto_checker(tmp_path):
    hooks = HookRegistry()
    hooks.register_checker(Gate.RUN_COMPLETE, _veto_on_sensitive_output, priority=80)

    agent = Agent(
        name="veto-clean",
        system_prompt="Answer directly.",
        config=AgentConfig(
            name="veto-clean",
            llm=script({"content": "The audit found no issues."}),
            hooks=hooks,
        ),
    )
    run = asyncio.run(agent.chat("summarise"))
    assert run.state == RunState.COMPLETED
