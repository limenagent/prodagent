from __future__ import annotations

import tempfile
from typing import TYPE_CHECKING, Any

import pytest

from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.backends.file.session_store import FileSessionStore
from prodagent.core.session import ConversationSession
from prodagent.kernel.state import AgentRun, child_run_id
from prodagent.kernel.types import RunState
from prodagent.playground.registry import RunReconstructError, RunRegistry

if TYPE_CHECKING:
    from pathlib import Path


class _FakeSpec:
    def __init__(self, name: str, built: list[str], fw: Any = None) -> None:
        self.name = name
        self.framework_config = fw

        def factory(run_id: str) -> Any:
            built.append((name, run_id))
            return ("agent-for", name, run_id)

        self.factory = factory


def _make_run(run_id: str, *, state: RunState = RunState.SUSPENDED) -> AgentRun:
    run = AgentRun(run_id=run_id, task="demo")
    run.state = state
    return run


def _registry(
    specs: list[_FakeSpec],
    *,
    checkpoints: dict[str, Any] | None = None,
    sessions: dict[str, Any] | None = None,
) -> RunRegistry:
    cp_map = checkpoints or {}
    ss_map = sessions or {}

    def _checkpoint(spec: Any) -> Any:
        if spec.name in cp_map:
            return cp_map[spec.name]
        return FileCheckpointStore(tempfile.mkdtemp(prefix=f"cp-{spec.name}-"))

    def _session(spec: Any) -> Any:
        if spec.name in ss_map:
            return ss_map[spec.name]
        return FileSessionStore(tempfile.mkdtemp(prefix=f"ss-{spec.name}-"))

    return RunRegistry(
        specs,  # type: ignore[arg-type]
        checkpoint_for=_checkpoint,
        session_store_for=_session,
    )


@pytest.mark.asyncio
async def test_reconstruct_unknown_run_raises_404(tmp_path: Path) -> None:
    cp_a = FileCheckpointStore(tmp_path / "a")
    cp_b = FileCheckpointStore(tmp_path / "b")
    reg = _registry(
        [_FakeSpec("a", []), _FakeSpec("b", [])],
        checkpoints={"a": cp_a, "b": cp_b},
    )
    with pytest.raises(RunReconstructError) as exc_info:
        await reg.reconstruct("nope-run-id")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_reconstruct_child_run_id_rejected(tmp_path: Path) -> None:
    cp = FileCheckpointStore(tmp_path / "only")
    reg = _registry([_FakeSpec("only", [])], checkpoints={"only": cp})
    child = child_run_id("root123", "peer")
    with pytest.raises(RunReconstructError) as exc_info:
        await reg.reconstruct(child)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_reconstruct_root_run_finds_example_and_rebuilds_agent(
    tmp_path: Path,
) -> None:
    cp_a = FileCheckpointStore(tmp_path / "a")
    cp_b = FileCheckpointStore(tmp_path / "b")
    built: list[tuple[str, str]] = []
    reg = _registry(
        [_FakeSpec("a", built), _FakeSpec("b", built)],
        checkpoints={"a": cp_a, "b": cp_b},
    )

    run = _make_run("cc1cb9174d99", state=RunState.SUSPENDED)
    run.pending_approval_id = "req-1"
    await cp_b.save(run)

    result = await reg.reconstruct("cc1cb9174d99")
    assert built == [("b", "cc1cb9174d99")]
    assert result.example_name == "b"
    assert result.run is not None
    assert result.run.run_id == "cc1cb9174d99"
    assert result.run.state is RunState.SUSPENDED
    assert result.run.pending_approval_id == "req-1"
    assert result.target_run_id == "cc1cb9174d99"
    assert result.session is None


@pytest.mark.asyncio
async def test_list_all_runs_skips_child_runs_and_consolidates(tmp_path: Path) -> None:
    cp_a = FileCheckpointStore(tmp_path / "a")
    cp_b = FileCheckpointStore(tmp_path / "b")
    reg = _registry(
        [_FakeSpec("a", []), _FakeSpec("b", [])],
        checkpoints={"a": cp_a, "b": cp_b},
    )

    await cp_a.save(_make_run("root1", state=RunState.COMPLETED))
    await cp_a.save(_make_run(child_run_id("root1", "peer"), state=RunState.COMPLETED))
    await cp_b.save(_make_run("root2", state=RunState.SUSPENDED))

    summaries = await reg.list_all_runs()
    ids = sorted(s.run_id for s in summaries)
    assert ids == ["root1", "root2"]
    example_map = {s.run_id: s.example for s in summaries}
    assert example_map == {"root1": "a", "root2": "b"}


@pytest.mark.asyncio
async def test_list_all_runs_tolerates_failing_store(tmp_path: Path) -> None:
    cp_ok = FileCheckpointStore(tmp_path / "ok")
    reg = _registry(
        [_FakeSpec("ok", []), _FakeSpec("boom", [])],
        checkpoints={"ok": cp_ok, "boom": _BoomStore()},
    )

    await cp_ok.save(_make_run("root_ok", state=RunState.COMPLETED))

    summaries = await reg.list_all_runs()
    assert any(s.run_id == "root_ok" for s in summaries)


@pytest.mark.asyncio
async def test_reconstruct_empty_run_id_rejected(tmp_path: Path) -> None:
    cp = FileCheckpointStore(tmp_path / "only")
    reg = _registry([_FakeSpec("only", [])], checkpoints={"only": cp})
    with pytest.raises(RunReconstructError) as exc_info:
        await reg.reconstruct("")
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_reconstruct_session_finds_owning_example(tmp_path: Path) -> None:
    cp_a = FileCheckpointStore(tmp_path / "cp-a")
    cp_b = FileCheckpointStore(tmp_path / "cp-b")
    ss_a = FileSessionStore(tmp_path / "ss-a")
    ss_b = FileSessionStore(tmp_path / "ss-b")
    built: list[tuple[str, str]] = []
    reg = _registry(
        [_FakeSpec("a", built), _FakeSpec("b", built)],
        checkpoints={"a": cp_a, "b": cp_b},
        sessions={"a": ss_a, "b": ss_b},
    )

    session = ConversationSession(session_id="sess-b-1", agent_id="b")
    await ss_b.save(session)

    result = await reg.reconstruct("sess-b-1")
    assert built == [("b", "sess-b-1")]
    assert result.example_name == "b"
    assert result.session is not None
    assert result.session.session_id == "sess-b-1"
    assert result.session.agent_id == "b"
    assert result.run is None


@pytest.mark.asyncio
async def test_reconstruct_session_unknown_raises_404(tmp_path: Path) -> None:
    ss_a = FileSessionStore(tmp_path / "ss-a")
    ss_b = FileSessionStore(tmp_path / "ss-b")
    reg = _registry(
        [_FakeSpec("a", []), _FakeSpec("b", [])],
        sessions={"a": ss_a, "b": ss_b},
    )
    with pytest.raises(RunReconstructError) as exc_info:
        await reg.reconstruct("no-such-session")
    assert exc_info.value.status_code == 404


class _BoomStore:
    async def list_run_ids(self) -> list[str]:
        raise RuntimeError("disk gone")

    async def load(self, run_id: str) -> AgentRun | None:  # noqa: ARG002
        raise RuntimeError("disk gone")
