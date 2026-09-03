"""Tape-attribution laws — member turns belong to the ensemble's tape.

A multi-agent orchestration opens one tape-root scope; every member's
session turn started inside it carries ``<root>::`` in its run id (the
convention spawned children already follow), so the WAL groups the whole
ensemble under one catalog entry instead of scattering member tapes.

Law 1: inside a root scope, a member chat turn's boundary facts land on
``<root>::…`` streams.

Law 2: outside any root scope, nothing is prefixed — a standalone chat
run is its own tape, unchanged.

Law 3: the prefix is deterministic on resume — the same session derives
the same tape-rooted id again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prodagent.backends.memory.event_log import InMemoryEventLog
from prodagent.base.config import FrameworkConfig
from prodagent.base.run_context import tape_root_scope
from prodagent.llm.fake import script
from prodagent.runtime.agent import Agent
from prodagent.runtime.config import AgentConfig

if TYPE_CHECKING:
    from pathlib import Path


def _member(log: InMemoryEventLog) -> Agent:
    fw = FrameworkConfig.default()
    fw.orchestration.runs_dir = ""  # bare: no checkpoint noise in this law
    fw.orchestration.sessions_dir = ""
    return Agent(
        "member",
        system_prompt="Reply briefly.",
        config=AgentConfig(
            name="member",
            llm=script({"content": "member reply"}),
            framework=fw,
            event_log=log,
        ),
    )


async def _turn(agent: Agent, session_id: str) -> str:
    run_id: str | None = None
    async for event in agent.chat_stream("hello", session_id=session_id):
        run = getattr(event, "run", None)
        if run is not None:
            run_id = run.run_id
    assert run_id is not None
    return run_id


async def test_member_turn_attributes_to_tape_root(tmp_path: Path) -> None:
    log = InMemoryEventLog()
    agent = _member(log)
    with tape_root_scope("ensemble-1"):
        run_id = await _turn(agent, "floor-a")
    assert run_id.startswith("ensemble-1::"), f"member turn prefixed, got {run_id}"
    streams = await log.list_streams()
    assert streams, "member facts landed"
    assert all(s.startswith("ensemble-1::") for s in streams), streams


async def test_standalone_turn_is_its_own_tape(tmp_path: Path) -> None:
    log = InMemoryEventLog()
    agent = _member(log)
    run_id = await _turn(agent, "solo")
    assert "::" not in run_id, "no root scope, no prefix"
    streams = await log.list_streams()
    assert all(not s.startswith("ensemble") for s in streams)


async def test_prefix_is_deterministic_across_turns(tmp_path: Path) -> None:
    log = InMemoryEventLog()
    agent = Agent(
        "member2",
        system_prompt="Reply briefly.",
        config=AgentConfig(
            name="member2",
            llm=script({"content": "r1"}, {"content": "r2"}),
            framework=FrameworkConfig.default(),
            event_log=log,
        ),
    )
    with tape_root_scope("ensemble-2"):
        first = await _turn(agent, "floor-b")
    with tape_root_scope("ensemble-2"):
        second = await _turn(agent, "floor-b")
    # Same session, same root: both turns share the root prefix (distinct
    # turn seqs), and the catalog groups them under one entry.
    assert first.startswith("ensemble-2::") and second.startswith("ensemble-2::")
    assert first != second
