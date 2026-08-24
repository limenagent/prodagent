"""Stateless resume — server restart must not strand a SUSPENDED run.

These tests simulate the bug report: user starts a run, hits an approval
window, the playground server restarts (in-process: each ``build_app()`` creates
a fresh ``AppState`` with an empty ``driving`` dict), then the user clicks
Approve. The endpoint must reconstruct the agent from the checkpoint store and
resume.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from prodagent.backends.file.checkpoint import FileCheckpointStore  # noqa: E402
from prodagent.backends.file.session_store import FileSessionStore  # noqa: E402
from prodagent.core.state.session import ConversationSession, TurnRecord  # noqa: E402
from prodagent.kernel.state import AgentRun, PendingHandoff  # noqa: E402
from prodagent.kernel.types import ExecutionMode, Message, RunState  # noqa: E402
from prodagent.playground import server as server_mod  # noqa: E402

if TYPE_CHECKING:
    from pathlib import Path


def _seed_suspended_run(checkpoint_dir: Path, run_id: str, task: str, request_id: str) -> None:
    cp = FileCheckpointStore(checkpoint_dir)
    run = AgentRun(run_id=run_id, task=task)
    run.state = RunState.SUSPENDED
    run.pending_approval_id = request_id
    run.messages = [Message(role="user", content=task)]
    import asyncio

    asyncio.run(cp.save(run))


def _patch_example(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    checkpoint_dir: Path,
    sessions_dir: Path | None = None,
) -> Any:
    """Build a fake spec carrying its own checkpoint/session stores.

    The spec exposes ``checkpoint_store`` / ``session_store`` attrs so
    ``_build_app`` can wire them via ``checkpoint_for`` / ``session_store_for``
    instead of patching module-level globals.
    """
    store = FileCheckpointStore(checkpoint_dir)

    session_store = None
    if sessions_dir is not None:
        session_store = FileSessionStore(sessions_dir)

        import prodagent.backends.factory as bf

        monkeypatch.setattr(bf, "resolve_session_store", lambda fw=None: session_store)

    class _StubAgent:
        def __init__(self, run_id: str) -> None:
            self.run_id = run_id
            self.approvals: list[tuple[str, str, str]] = []
            self.hooks = None  # _attach_web_hooks will attach a fresh registry.
            self._session_store: Any = None

        def attach_default_hooks(self):  # noqa: ANN001
            from prodagent.kernel.bus import HookRegistry

            self.hooks = HookRegistry()
            return self.hooks

        def _ensure_session_store_resolved(self):  # noqa: ANN001
            if self._session_store is None:
                from prodagent.backends.factory import resolve_session_store

                self._session_store = resolve_session_store(None)
            return self._session_store

        async def submit_approval(
            self, request_id: str, decision: str, *, approver_id: str = ""
        ) -> None:
            self.approvals.append((request_id, decision, approver_id))

        async def stream(self, task: str, *, run_id: str):  # noqa: ARG002
            from prodagent.kernel.events import RunCompletedEvent

            completed_run = AgentRun(run_id=run_id, task=task)
            completed_run.state = RunState.COMPLETED
            completed_run.final_output = "ok"
            yield RunCompletedEvent(run=completed_run)

        async def chat_stream(self, message: str, *, session_id: str):  # noqa: ARG002
            from prodagent.kernel.events import RunCompletedEvent

            completed_run = AgentRun(run_id=session_id, task=message)
            completed_run.state = RunState.COMPLETED
            completed_run.final_output = f"chat:{message}"
            yield RunCompletedEvent(run=completed_run)

        async def chat_resume_stream(self, *, session_id: str):  # noqa: ARG002
            from prodagent.kernel.events import RunCompletedEvent

            completed_run = AgentRun(run_id=session_id, task="resumed")
            completed_run.state = RunState.COMPLETED
            completed_run.final_output = "resumed"
            yield RunCompletedEvent(run=completed_run)

    class _Spec:
        def __init__(self, n: str) -> None:
            self.name = n
            self.number = 0
            self.title = n
            self.description = ""
            self.default_task = "demo task"
            self.is_hitl = True

        def factory(self, run_id: str) -> Any:
            return _StubAgent(run_id)

        def to_dict(self) -> dict[str, Any]:
            return {
                "name": self.name,
                "number": 0,
                "title": self.name,
                "description": "",
                "default_task": self.default_task,
                "is_hitl": True,
            }

    spec = _Spec(name)
    spec.checkpoint_store = store
    spec.session_store = session_store
    spec.framework_config = None
    return spec


def _build_app(spec: Any) -> Any:
    return server_mod.build_app(
        specs=[spec],
        checkpoint_for=lambda _: spec.checkpoint_store,
        session_store_for=lambda _: spec.session_store,
    )


def test_approve_after_simulated_restart_resumes_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "cc1cb9174d99"
    request_id = "req-1"
    task = "支付服务有告警"
    checkpoint_dir = tmp_path / "aiops-checkpoints"
    _seed_suspended_run(checkpoint_dir, run_id, task, request_id)
    spec = _patch_example(monkeypatch, "aiops", checkpoint_dir)

    app = _build_app(spec)
    client = TestClient(app)

    # Simulate server restart: _DRIVING is empty (guaranteed by fixture).
    assert app.state.playground.driving.get(run_id) is None

    resp = client.post(
        "/api/approve",
        json={"run_id": run_id, "request_id": request_id, "decision": "approve"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "resuming", "run_id": run_id}

    # The reconstructed agent should have received the approval decision.
    ctx = app.state.playground.driving.get(run_id)
    # _drive_run may have already terminated and evicted; if so, the approval was
    # still submitted synchronously before drive started.
    # If still driving, check the stub captured the approval.
    if ctx is not None:
        agent = ctx.agent
        assert ("req-1", "approve", "web") in agent.approvals


def test_approve_unknown_run_returns_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkpoint_dir = tmp_path / "empty"
    checkpoint_dir.mkdir()
    spec = _patch_example(monkeypatch, "aiops", checkpoint_dir)

    app = _build_app(spec)
    client = TestClient(app)

    resp = client.post(
        "/api/approve",
        json={"run_id": "never-existed", "request_id": "r", "decision": "approve"},
    )
    assert resp.status_code == 404
    assert "unknown run" in resp.json()["detail"]


def test_approve_child_run_id_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkpoint_dir = tmp_path / "empty"
    checkpoint_dir.mkdir()
    spec = _patch_example(monkeypatch, "aiops", checkpoint_dir)

    app = _build_app(spec)
    client = TestClient(app)

    resp = client.post(
        "/api/approve",
        json={"run_id": "root::peer", "request_id": "r", "decision": "approve"},
    )
    assert resp.status_code == 400
    assert "child" in resp.json()["detail"].lower()


def test_approve_invalid_decision_returns_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint_dir = tmp_path / "empty"
    checkpoint_dir.mkdir()
    spec = _patch_example(monkeypatch, "aiops", checkpoint_dir)

    app = _build_app(spec)
    client = TestClient(app)

    resp = client.post(
        "/api/approve",
        json={"run_id": "any", "request_id": "r", "decision": "maybe"},
    )
    assert resp.status_code == 400


def test_runs_listing_returns_suspended_and_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint_dir = tmp_path / "mixed"
    cp = FileCheckpointStore(checkpoint_dir)

    import asyncio

    suspended = AgentRun(run_id="root-suspended", task="t1")
    suspended.state = RunState.SUSPENDED
    suspended.pending_approval_id = "req-x"
    completed = AgentRun(run_id="root-done", task="t2")
    completed.state = RunState.COMPLETED
    completed.final_output = "done"
    asyncio.run(cp.save(suspended))
    asyncio.run(cp.save(completed))

    spec = _patch_example(monkeypatch, "aiops", checkpoint_dir)

    app = _build_app(spec)
    client = TestClient(app)

    resp = client.get("/api/runs")
    assert resp.status_code == 200
    runs = {r["run_id"]: r for r in resp.json()}
    assert runs["root-suspended"]["state"] == "suspended"
    assert runs["root-suspended"]["pending_approval_id"] == "req-x"
    assert runs["root-done"]["state"] == "completed"
    assert runs["root-done"]["final_output"] == "done"


def test_stream_history_replay_for_unknown_driving_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoint_dir = tmp_path / "history"
    cp = FileCheckpointStore(checkpoint_dir)

    import asyncio

    completed = AgentRun(run_id="root-hist", task="t")
    completed.state = RunState.COMPLETED
    completed.final_output = "history output"
    asyncio.run(cp.save(completed))

    spec = _patch_example(monkeypatch, "aiops", checkpoint_dir)

    app = _build_app(spec)
    client = TestClient(app)

    with client.stream("GET", "/api/stream/root-hist") as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes()).decode()
    assert "completed" in body
    assert "history output" in body


def test_approve_on_already_completed_run_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Double-click or stale modal: a second approve on a COMPLETED run must
    return 200 (idempotent), not 409 — the user's intent (let it proceed) is
    already the current state."""
    checkpoint_dir = tmp_path / "completed-run"
    cp = FileCheckpointStore(checkpoint_dir)

    import asyncio

    completed = AgentRun(run_id="cc1cb9174d99", task="t")
    completed.state = RunState.COMPLETED
    completed.final_output = "done"
    asyncio.run(cp.save(completed))

    spec = _patch_example(monkeypatch, "aiops", checkpoint_dir)

    app = _build_app(spec)
    client = TestClient(app)

    resp = client.post(
        "/api/approve",
        json={"run_id": "cc1cb9174d99", "request_id": "req-stale", "decision": "approve"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"].startswith("already_")


def test_reject_on_already_completed_run_returns_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rejecting a run that already completed is a real conflict — the user's
    intent (stop the tool) cannot be applied retroactively. Must return 409,
    not the idempotent 200 that approve-direction gets."""
    checkpoint_dir = tmp_path / "completed-run-reject"
    cp = FileCheckpointStore(checkpoint_dir)

    import asyncio

    completed = AgentRun(run_id="cc1cb9174d99", task="t")
    completed.state = RunState.COMPLETED
    completed.final_output = "done"
    asyncio.run(cp.save(completed))

    spec = _patch_example(monkeypatch, "aiops", checkpoint_dir)

    app = _build_app(spec)
    client = TestClient(app)

    resp = client.post(
        "/api/approve",
        json={"run_id": "cc1cb9174d99", "request_id": "req-stale", "decision": "reject"},
    )
    assert resp.status_code == 409
    assert "reject cannot be applied" in resp.json()["detail"]


def test_approve_completed_root_with_suspended_peer_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root COMPLETED with pending_handoff to a SUSPENDED peer must be treated
    as resumable, not terminal. The orchestrator's _find_suspended_peer handles
    the resume transparently — the approve endpoint just needs to not
    short-circuit on the root's COMPLETED state."""
    checkpoint_dir = tmp_path / "peer-handoff"
    cp = FileCheckpointStore(checkpoint_dir)

    import asyncio

    root_run_id = "aiops-peer-demo"
    peer_run_id = "aiops-peer-demo::remediator"
    request_id = "req-peer-1"
    task = "支付服务有告警"

    root = AgentRun(run_id=root_run_id, task=task)
    root.state = RunState.COMPLETED
    root.final_output = '{"recommended_action": "rollback"}'
    root.pending_handoff = PendingHandoff(
        peer_name="remediator", task="remediate", peer_run_id=peer_run_id
    )
    asyncio.run(cp.save(root))

    peer = AgentRun(run_id=peer_run_id, task="remediate")
    peer.state = RunState.SUSPENDED
    peer.pending_approval_id = request_id
    asyncio.run(cp.save(peer))

    spec = _patch_example(monkeypatch, "aiops", checkpoint_dir)

    app = _build_app(spec)
    client = TestClient(app)

    # Server restarted — _DRIVING is empty (guaranteed by fixture).
    assert app.state.playground.driving.get(root_run_id) is None

    resp = client.post(
        "/api/approve",
        json={
            "run_id": root_run_id,
            "request_id": request_id,
            "decision": "approve",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "resuming", "run_id": root_run_id}

    # The reconstructed agent should have received the approval decision, which
    # the orchestrator will apply to the peer's ApprovalGate when it resumes.
    ctx = app.state.playground.driving.get(root_run_id)
    if ctx is not None:
        agent = ctx.agent
        assert (request_id, "approve", "web") in agent.approvals


def test_reconstruct_target_run_id_is_self_for_genuinely_terminal_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A COMPLETED root with no pending_handoff has no suspended peer —
    reconstruct's target_run_id equals the input run_id, so the approve endpoint
    falls through to the idempotent 'already_completed' path."""
    checkpoint_dir = tmp_path / "truly-completed"
    cp = FileCheckpointStore(checkpoint_dir)

    import asyncio

    completed = AgentRun(run_id="truly-done", task="t")
    completed.state = RunState.COMPLETED
    completed.final_output = "done"
    asyncio.run(cp.save(completed))

    spec = _patch_example(monkeypatch, "aiops", checkpoint_dir)

    from prodagent.playground.registry import RunRegistry

    registry = RunRegistry(
        [spec],
        checkpoint_for=lambda _: spec.checkpoint_store,
        session_store_for=lambda _: spec.session_store,
    )
    result = asyncio.run(registry.reconstruct("truly-done"))
    assert result.target_run_id == "truly-done"
    assert result.run is not None
    assert result.run.state is RunState.COMPLETED


def test_reconstruct_target_run_id_is_peer_for_handoff_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root COMPLETED + pending_handoff + peer SUSPENDED → target_run_id is the peer."""
    checkpoint_dir = tmp_path / "handoff-suspended"
    cp = FileCheckpointStore(checkpoint_dir)

    import asyncio

    root_run_id = "handoff-root"
    peer_run_id = "handoff-root::remediator"

    root = AgentRun(run_id=root_run_id, task="t")
    root.state = RunState.COMPLETED
    root.pending_handoff = PendingHandoff(
        peer_name="remediator", task="remediate", peer_run_id=peer_run_id
    )
    asyncio.run(cp.save(root))

    peer = AgentRun(run_id=peer_run_id, task="remediate")
    peer.state = RunState.SUSPENDED
    asyncio.run(cp.save(peer))

    spec = _patch_example(monkeypatch, "aiops", checkpoint_dir)

    from prodagent.playground.registry import RunRegistry

    registry = RunRegistry(
        [spec],
        checkpoint_for=lambda _: spec.checkpoint_store,
        session_store_for=lambda _: spec.session_store,
    )
    result = asyncio.run(registry.reconstruct(root_run_id))
    assert result.target_run_id == peer_run_id


def test_chat_after_restart_rebuilds_agent_from_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """chat on a session_id unknown to _DRIVING must rebuild the agent from the
    example factory — NOT probe the checkpoint store (the run_id is the session_id;
    the real run_id ``session_id:N`` lives in the session store, not checkpoint)."""
    checkpoint_dir = tmp_path / "chat-empty"
    checkpoint_dir.mkdir()
    spec = _patch_example(monkeypatch, "trader", checkpoint_dir)

    app = _build_app(spec)
    client = TestClient(app)

    resp = client.post(
        "/api/chat",
        json={
            "example": "trader",
            "run_id": "session-after-restart",
            "message": "再来一杯",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["run_id"] == "session-after-restart"


def test_chat_unknown_example_returns_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkpoint_dir = tmp_path / "chat-unknown"
    checkpoint_dir.mkdir()
    spec = _patch_example(monkeypatch, "trader", checkpoint_dir)

    app = _build_app(spec)
    client = TestClient(app)

    resp = client.post(
        "/api/chat",
        json={
            "example": "no-such-example",
            "run_id": "whatever",
            "message": "hi",
        },
    )
    assert resp.status_code == 404
    assert "unknown example" in resp.json()["detail"]


def test_approve_after_restart_uses_session_to_find_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: approve on a chat run_id (which is a session_id) after a
    server restart must reconstruct via the SESSION store, not by probing the
    checkpoint store with the session_id as if it were a run_id.

    The bug: ``_REGISTRY.reconstruct(session_id)`` scans every example's
    checkpoint store for ``run_id == session_id`` — but for chat runs the
    session_id is NOT a checkpoint key. The real run_id is ``session_id:N``
    (stored on ``session.last_turn.run_id``), and the suspended checkpoint
    lives under THAT key. So the checkpoint probe returns None and approve
    404s on a run that visibly exists in /api/runs.
    """
    import asyncio

    checkpoint_dir = tmp_path / "chat-approve-restart"
    checkpoint_dir.mkdir()
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    session_id = "trader-after-restart"
    real_run_id = f"{session_id}:1"
    request_id = "req-chat-1"
    task = "订 10 杯奶茶"

    cp = FileCheckpointStore(checkpoint_dir)
    suspended_run = AgentRun(run_id=real_run_id, task=task)
    suspended_run.state = RunState.SUSPENDED
    suspended_run.pending_approval_id = request_id
    suspended_run.messages = [Message(role="user", content=task)]
    asyncio.run(cp.save(suspended_run))

    session = ConversationSession(session_id=session_id, agent_id="trader")
    session.turns = [
        TurnRecord(
            run_id=real_run_id,
            mode=ExecutionMode.REACTIVE,
            state=RunState.SUSPENDED,
        )
    ]
    session.turn_seq = 1
    session.messages = [Message(role="user", content=task)]
    session_store = FileSessionStore(sessions_dir)
    asyncio.run(session_store.save(session))

    spec = _patch_example(monkeypatch, "trader", checkpoint_dir, sessions_dir)

    app = _build_app(spec)
    client = TestClient(app)

    # Restart: _DRIVING is empty (guaranteed by fixture).
    assert app.state.playground.driving.get(session_id) is None

    resp = client.post(
        "/api/approve",
        json={
            "run_id": session_id,
            "request_id": request_id,
            "decision": "approve",
        },
    )
    # The bug returns 404 "unknown run"; the fix returns 200 "resuming".
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "resuming", "run_id": session_id}

    ctx = app.state.playground.driving.get(session_id)
    if ctx is not None:
        agent = ctx.agent
        assert (request_id, "approve", "web") in agent.approvals
