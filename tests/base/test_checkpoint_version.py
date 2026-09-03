from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from prodagent import CorruptedCheckpointError, VersionConflict
from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.kernel.run import Run

if TYPE_CHECKING:
    from pathlib import Path


def _run(run_id: str = "R1") -> Run:
    return Run(run_id=run_id, task="t")


@pytest.mark.asyncio
async def test_file_version_conflict_on_concurrent_save(tmp_path: Path):
    store = FileCheckpointStore(directory=tmp_path)
    await store.save(_run())
    with pytest.raises(VersionConflict):
        await store.save(_run(), expected_version=0)


@pytest.mark.asyncio
async def test_file_expected_version_match_saves(tmp_path: Path):
    store = FileCheckpointStore(directory=tmp_path)
    await store.save(_run())
    assert (await store.load("R1")).checkpoint_version == 1
    await store.save(_run(), expected_version=1)
    assert (await store.load("R1")).checkpoint_version == 2


@pytest.mark.asyncio
async def test_file_corrupt_json_raises_on_load(tmp_path: Path):
    store = FileCheckpointStore(directory=tmp_path)
    (tmp_path / "R1.v1.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(CorruptedCheckpointError):
        await store.load("R1")


@pytest.mark.asyncio
async def test_file_corrupt_schema_raises_on_load(tmp_path: Path):
    store = FileCheckpointStore(directory=tmp_path)
    (tmp_path / "R1.v1.json").write_text(
        json.dumps({"version": 1, "run": {"task": "t"}}),
        encoding="utf-8",
    )
    with pytest.raises(CorruptedCheckpointError):
        await store.load("R1")


@pytest.mark.asyncio
async def test_file_corrupt_history_version_does_not_block_new_save(tmp_path: Path):
    store = FileCheckpointStore(directory=tmp_path)
    (tmp_path / "R1.v1.json").write_text("{garbage", encoding="utf-8")
    await store.save(_run())
    loaded = await store.load("R1")
    assert loaded is not None
    assert loaded.checkpoint_version == 2
    with pytest.raises(CorruptedCheckpointError):
        await store.load("R1", version=1)


@pytest.mark.asyncio
async def test_file_concurrent_saves_do_not_silently_overwrite(tmp_path: Path):
    store_a = FileCheckpointStore(directory=tmp_path)
    store_b = FileCheckpointStore(directory=tmp_path)

    barrier = asyncio.Barrier(2)

    async def saver(store: FileCheckpointStore) -> str:
        await barrier.wait()
        try:
            await store.save(_run(), expected_version=0)
            return "ok"
        except VersionConflict:
            return "conflict"

    results = await asyncio.gather(saver(store_a), saver(store_b))
    assert sorted(results) == ["conflict", "ok"], (
        f"expected one ok + one conflict, got {results} — "
        "concurrent saves raced past the version check"
    )

    loaded = await store_a.load("R1")
    assert loaded is not None
    assert loaded.checkpoint_version == 1


@pytest.mark.asyncio
async def test_file_oserror_on_write_sets_checkpoint_failed_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import prodagent.backends.file.checkpoint as checkpoint_module

    def _boom(*_a, **_k):
        raise OSError("simulated disk full")

    monkeypatch.setattr(checkpoint_module, "write_atomic_json", _boom)

    store = FileCheckpointStore(directory=tmp_path)
    run = _run()
    assert run.checkpoint_failed is False

    await store.save(run)

    assert run.checkpoint_failed is True


@pytest.mark.asyncio
async def test_file_concurrent_saves_without_expected_version_serialise(tmp_path: Path):
    store_a = FileCheckpointStore(directory=tmp_path)
    store_b = FileCheckpointStore(directory=tmp_path)

    barrier = asyncio.Barrier(2)

    async def saver(store: FileCheckpointStore) -> None:
        await barrier.wait()
        await store.save(_run())

    await asyncio.gather(saver(store_a), saver(store_b))

    loaded = await store_a.load("R1")
    assert loaded is not None
    assert loaded.checkpoint_version == 2
