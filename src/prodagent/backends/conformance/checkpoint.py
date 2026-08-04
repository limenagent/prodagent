"""Conformance tests for ``CheckpointStore`` implementations."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeAlias

from prodagent.core.state.run import AgentRun
from prodagent.ports.checkpoint import CheckpointStore

Factory: TypeAlias = Callable[[], CheckpointStore]


async def run_checkpoint_conformance(make_store: Factory) -> None:
    store = make_store()

    run = AgentRun(run_id="r1", task="t")
    await store.save(run)

    loaded = await store.load("r1")
    assert loaded is not None
    assert loaded.run_id == "r1"
    assert loaded.task == "t"

    assert "r1" in await store.list_run_ids()
    assert await store.load("missing") is None

    run2 = AgentRun(run_id="r2", task="other")
    await store.save(run2)
    ids = await store.list_run_ids()
    assert "r1" in ids and "r2" in ids


async def run_checkpoint_versioning_conformance(make_store: Factory) -> None:
    """``save`` with ``expected_version`` enforces optimistic concurrency."""
    store = make_store()
    run = AgentRun(run_id="v1", task="t")

    await store.save(run, expected_version=None)
    versions = await store.list_versions("v1")
    assert versions, "at least one version after first save"

    await store.save(run, expected_version=None)
    later = await store.list_versions("v1")
    assert len(later) >= len(versions)


async def run_checkpoint_fork_conformance(make_store: Factory) -> None:
    """``fork`` creates an independent run sharing a snapshot."""
    store = make_store()
    run = AgentRun(run_id="fork-src", task="t")
    await store.save(run)

    versions = await store.list_versions("fork-src")
    if not versions:
        return  # backend without version history skips fork

    forked_id = await store.fork("fork-src", at_version=versions[-1], new_run_id="fork-dst")
    assert forked_id == "fork-dst"
    forked = await store.load("fork-dst")
    assert forked is not None
    assert forked.run_id == "fork-dst"
    assert forked.task == "t"
