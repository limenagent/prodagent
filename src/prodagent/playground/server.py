"""FastAPI app — the playground HTTP surface.

Stateless: run state lives in checkpoint/session stores, not in process memory.
``AppState.driving`` only caches runs whose ``agent.chat_stream()`` coroutine is
currently being driven by this process — it is a concurrency guard, not the
existence truth. Any run_id unknown to ``driving`` is reverse-looked-up via
``RunRegistry.reconstruct``, which probes both session and checkpoint stores.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
except ImportError as exc:  # pragma: no cover - exercised via missing-dep test
    raise RuntimeError(
        "The playground needs FastAPI, which is not part of the thin core. "
        "Install it with: pip install 'prodagent[playground]' "
        "(or 'pip install fastapi uvicorn')"
    ) from exc

from prodagent.kernel.types import (
    RunCompletedEvent,
    RunFailedEvent,
    RunState,
    RunSuspendedEvent,
)
from prodagent.playground.multiagent import MultiRun
from prodagent.playground.registry import (
    CheckpointFactory,
    RunReconstructError,
    RunRegistry,
    SessionStoreFactory,
    discover_examples,
)
from prodagent.playground.web_hooks import WebPushHooks

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from starlette.responses import Response
    from starlette.types import Scope

    from prodagent.base.session import ConversationSession
    from prodagent.kernel.run import Run
    from prodagent.ports.observability import EventLog
    from prodagent.ports.persistence import BlobStore
    from prodagent.runtime.agent import Agent

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"

_SSE_HEARTBEAT_S = 15.0


@dataclass
class RunContext:
    """Per-run driver state — only kept in memory while this process is driving the run."""

    agent: Agent
    queue: asyncio.Queue[dict[str, Any]]
    task: str
    example_name: str
    run_id: str
    final_output: str | None = None
    error: str | None = None
    suspended: bool = False
    pending_request_id: str | None = None
    driving: bool = False
    streamers: int = 0
    terminal_delivered: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def done(self) -> bool:
        return not self.suspended


@dataclass
class AppState:
    specs: list[Any] = field(default_factory=discover_examples)
    registry: RunRegistry | None = None
    driving: dict[str, RunContext] = field(default_factory=dict)
    tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict)
    checkpoint_for: CheckpointFactory | None = None
    session_store_for: SessionStoreFactory | None = None
    tape_event_log: EventLog | None = None
    multiagent_runs: dict[str, MultiRun] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.registry is None:
            self.registry = RunRegistry(
                self.specs,
                checkpoint_for=self.checkpoint_for,
                session_store_for=self.session_store_for,
            )

    def spec_for(self, example: str) -> Any | None:
        return next((s for s in self.specs if s.name == example), None)

    def build_ctx(self, agent: Agent, run_id: str, task: str, example_name: str) -> RunContext:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        _attach_web_hooks(agent, queue)
        ctx = RunContext(
            agent=agent,
            queue=queue,
            task=task,
            example_name=example_name,
            run_id=run_id,
        )
        self.driving[run_id] = ctx
        return ctx

    def spawn_drive(self, coro: Any, run_id: str) -> None:
        task = asyncio.create_task(coro)
        self.tasks[run_id] = task

    async def reconstruct_ctx_for_session(
        self, session_id: str, *, example: str, task: str
    ) -> RunContext:
        spec = self.spec_for(example)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"unknown example: {example!r}")
        agent = spec.factory(session_id)
        return self.build_ctx(agent, session_id, task, example)

    async def reconstruct_for_approve(
        self, run_id: str, decision: str
    ) -> RunContext | dict[str, str]:
        """Approve/reject a run this process may never have driven: reconstruct
        from storage, and treat "already settled" as an idempotent success for
        approve but a 409 for reject (a reject arriving after completion can't
        un-run the tool — the caller must know)."""
        try:
            result = await self.registry.reconstruct(run_id)  # type: ignore[union-attr]
        except RunReconstructError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

        if result.session is not None:
            last = result.session.last_turn
            if last is None or last.state is not RunState.SUSPENDED:
                state = last.state.value.lower() if last else "empty"
                if decision == "reject":
                    raise HTTPException(
                        status_code=409,
                        detail=f"run {run_id} already {state} — reject cannot be applied",
                    )
                return {"status": f"already_{state}", "run_id": run_id}
            return self.build_ctx(
                result.agent, run_id, _task_from_session(result.session), result.example_name
            )

        run = result.run
        if run is None:
            raise HTTPException(status_code=404, detail=f"unknown run: {run_id}")

        if run.state is not RunState.SUSPENDED and result.target_run_id == run_id:
            if decision == "reject":
                raise HTTPException(
                    status_code=409,
                    detail=f"run {run_id} already {run.state.value.lower()} — reject cannot be applied",
                )
            return {"status": f"already_{run.state.value.lower()}", "run_id": run_id}
        return self.build_ctx(result.agent, run_id, _task_from_run(run), result.example_name)

    async def drive_stream(
        self,
        ctx: RunContext,
        run_id: str,
        stream_method: Any,
        *,
        label: str,
    ) -> None:
        from prodagent.base.run_context import run_scope

        with run_scope(run_id, self.tape_event_log):
            await self._drive_stream_inner(ctx, run_id, stream_method, label=label)

    async def _drive_stream_inner(
        self,
        ctx: RunContext,
        run_id: str,
        stream_method: Any,
        *,
        label: str,
    ) -> None:
        """Pump one run's event stream onto its SSE queue until a terminal
        event lands. Handoff continuations are skipped here — the chain
        driver owns them; this surface reports each hop's own ending."""
        ctx.driving = True
        try:
            async for event in stream_method(run_id=run_id):
                if isinstance(event, RunSuspendedEvent):
                    # Park: surface the approval id so the UI can render the
                    # approve/reject controls; the stream ends here.
                    ctx.suspended = True
                    ctx.pending_request_id = event.run.pending_approval_id
                    await ctx.queue.put(
                        {
                            "type": "suspended",
                            "run_id": run_id,
                            "request_id": ctx.pending_request_id,
                        }
                    )
                    return
                if isinstance(event, RunCompletedEvent):
                    if event.run.pending_handoff is not None:
                        continue  # mid-chain hop completion — the chain's own ending follows
                    ctx.final_output = event.run.final_output
                    await ctx.queue.put(
                        {
                            "type": "completed",
                            "final_output": ctx.final_output,
                            "state": event.run.state.value,
                        }
                    )
                    return
                if isinstance(event, RunFailedEvent):
                    ctx.error = event.error
                    await ctx.queue.put({"type": "failed", "error": ctx.error})
                    return
        except Exception as exc:
            logger.exception("[playground] %s %s crashed", label, run_id)
            ctx.error = f"{type(exc).__name__}: {exc}"
            await ctx.queue.put({"type": "failed", "error": ctx.error})
        finally:
            ctx.driving = False
            self.tasks.pop(run_id, None)
            if ctx.done:
                if ctx.streamers > 0:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(ctx.terminal_delivered.wait(), timeout=5.0)
                self.driving.pop(run_id, None)

    async def drive(
        self, ctx: RunContext, run_id: str, message: str, *, reactive: bool = False
    ) -> None:
        await self.drive_stream(
            ctx,
            run_id,
            lambda **_: ctx.agent.chat_stream(message, session_id=run_id),
            label="chat",
        )

    async def drive_resume(self, ctx: RunContext, run_id: str) -> None:
        await self.drive_stream(
            ctx,
            run_id,
            lambda **_: ctx.agent.chat_stream(session_id=run_id, resume=True),
            label="resume",
        )


def _attach_web_hooks(agent: Agent, queue: asyncio.Queue[dict[str, Any]]) -> None:
    registry = agent.hooks
    if registry is None:
        registry = agent.attach_default_hooks()
    if registry is None:
        raise RuntimeError("Agent has no hooks registry and attach_default_hooks returned None")
    WebPushHooks(queue).attach(registry)


def _task_from_run(run: Run) -> str:
    for msg in run.messages:
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
        if role == "user" and isinstance(content, str) and content:
            return content
    return ""


def _task_from_session(session: ConversationSession) -> str:
    for msg in reversed(session.messages):
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
        if role == "user" and isinstance(content, str) and content:
            return content
    return ""


class _NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


def build_app(
    *,
    specs: list[Any] | None = None,
    checkpoint_for: CheckpointFactory | None = None,
    session_store_for: SessionStoreFactory | None = None,
    event_log: EventLog | None = None,
    blob_store: BlobStore | None = None,
) -> FastAPI:
    state = AppState(
        specs=specs or discover_examples(),
        checkpoint_for=checkpoint_for,
        session_store_for=session_store_for,
    )
    state.tape_event_log = event_log  # set below when defaulted
    # The tape deck's data source: the same WAL the driven runs record to.
    # Default = the production profile's file stores (same directories the
    # runs write, read through separate instances — the file backend is the
    # shared medium); tests inject in-memory ones.
    if event_log is None:
        from prodagent.backends.file.blob import FileBlobStore
        from prodagent.backends.file.event_log import FileEventLog
        from prodagent.base.config import FrameworkConfig

        _fw = FrameworkConfig.from_env()
        event_log = FileEventLog(_fw.orchestration.events_dir)
        blob_store = blob_store or FileBlobStore(_fw.blobs_dir)
    state.tape_event_log = event_log

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        yield
        if state.registry is not None:
            await state.registry.aclose()

    app = FastAPI(title="prodagent playground", lifespan=lifespan)
    from prodagent.playground.tape import build_tape_router

    app.include_router(build_tape_router(state, event_log, blob_store))
    app.state.playground = state

    if _STATIC_DIR.exists():
        app.mount("/static", _NoCacheStaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.post("/api/multiagent/{example}/start")
    async def multiagent_start(example: str) -> dict[str, str]:
        spec = state.spec_for(example)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"unknown example: {example!r}")
        if spec.multiagent_adapter is None:
            raise HTTPException(
                status_code=404,
                detail=f"{example!r} is single-agent only — use /api/run instead",
            )
        adapter = spec.multiagent_adapter()
        run_id = uuid.uuid4().hex[:12]
        run = MultiRun(adapter, run_id=run_id, event_log=state.tape_event_log)
        state.multiagent_runs[run_id] = run
        run.start()
        return {"run_id": run_id}

    @app.get("/api/multiagent/{example}/stream/{run_id}")
    async def multiagent_stream(example: str, run_id: str) -> StreamingResponse:
        run = state.multiagent_runs.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"unknown run: {run_id}")

        async def event_stream() -> Any:
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(run.queue.get(), timeout=_SSE_HEARTBEAT_S)
                    except TimeoutError:
                        yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
                        continue
                    yield f"data: {json.dumps(event, default=str, ensure_ascii=False)}\n\n"
                    if event.get("kind") in ("completed", "failed"):
                        return
            finally:
                # The run keeps driving to completion server-side even if the
                # client disconnects; we just stop streaming to this client.
                pass

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.get("/api/examples")
    async def list_examples() -> list[dict[str, Any]]:
        return [spec.to_dict() for spec in state.specs]

    @app.get("/api/runs")
    async def list_runs() -> list[dict[str, Any]]:
        summaries = await state.registry.list_all_runs()  # type: ignore[union-attr]
        return [s.to_dict() for s in summaries]

    @app.post("/api/run")
    async def start_run(body: dict[str, Any]) -> dict[str, str]:
        example = body.get("example")
        task = body.get("task", "")
        if not isinstance(example, str):
            raise HTTPException(status_code=400, detail="missing or invalid 'example' field")
        spec = state.spec_for(example)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"unknown example: {example!r}")
        if spec.factory is None:
            raise HTTPException(
                status_code=404,
                detail=f"{example!r} is multi-agent only — use /api/multiagent/{example}/start",
            )
        if not task:
            task = spec.default_task
        run_id = body.get("run_id") or uuid.uuid4().hex[:12]
        try:
            agent = spec.factory(run_id)
        except Exception as exc:
            logger.exception("[playground] factory %s failed", example)
            raise HTTPException(status_code=500, detail=f"factory error: {exc}") from exc
        ctx = state.build_ctx(agent, run_id, task, example)
        state.spawn_drive(state.drive(ctx, run_id, task), run_id)
        return {"run_id": run_id}

    @app.post("/api/approve")
    async def approve(body: dict[str, Any]) -> dict[str, str]:
        run_id = body.get("run_id", "")
        request_id = body.get("request_id", "")
        decision = body.get("decision", "")
        if decision not in ("approve", "reject"):
            raise HTTPException(status_code=400, detail=f"invalid decision: {decision!r}")

        ctx = state.driving.get(run_id)
        if ctx is None:
            rebuilt = await state.reconstruct_for_approve(run_id, decision)
            if isinstance(rebuilt, dict):
                return rebuilt
            ctx = rebuilt
        elif not ctx.suspended:
            if decision == "reject":
                raise HTTPException(
                    status_code=409,
                    detail=f"run {run_id} already resumed — reject cannot be applied",
                )
            return {"status": "already_resumed", "run_id": run_id}

        await ctx.agent.submit_approval(
            request_id,
            decision,
            approver_id=body.get("approver") or "web",
        )
        ctx.suspended = False
        ctx.pending_request_id = None
        state.spawn_drive(state.drive_resume(ctx, run_id), run_id)
        return {"status": "resuming", "run_id": run_id}

    @app.post("/api/chat")
    async def chat(body: dict[str, Any]) -> dict[str, str]:
        # An omitted run_id opens a NEW conversation: mint the session id
        # here and return it, so the caller can follow (and continue) it —
        # an empty string would surface as a tape lookup miss downstream.
        run_id = body.get("run_id") or uuid.uuid4().hex[:12]
        message = body.get("message", "")
        example = body.get("example", "")
        if not message:
            raise HTTPException(status_code=400, detail="message is required")

        ctx = state.driving.get(run_id)
        if ctx is None:
            ctx = await state.reconstruct_ctx_for_session(run_id, example=example, task=message)
        elif ctx.driving:
            raise HTTPException(status_code=409, detail="run is already driving")
        elif ctx.suspended:
            raise HTTPException(
                status_code=409,
                detail="run is suspended — approve or reject before chatting",
            )
        state.spawn_drive(state.drive(ctx, run_id, message, reactive=True), run_id)
        return {"run_id": run_id}

    @app.get("/api/stream/{run_id}")
    async def stream_run(run_id: str) -> StreamingResponse:
        ctx = state.driving.get(run_id)
        if ctx is not None:
            return _stream_from_ctx(ctx)

        summary = await state.registry.load_summary(run_id)  # type: ignore[union-attr]
        if summary is None:
            raise HTTPException(status_code=404, detail=f"unknown run: {run_id}")

        async def history_stream() -> Any:
            payload: dict[str, Any] = {
                "run_id": run_id,
                "state": summary.state.value,
            }
            if summary.state is RunState.SUSPENDED:
                payload["type"] = "suspended"
                payload["request_id"] = summary.pending_approval_id
            elif summary.state is RunState.COMPLETED:
                payload["type"] = "completed"
                payload["final_output"] = summary.final_output
            elif summary.state is RunState.FAILED:
                payload["type"] = "failed"
                payload["error"] = summary.last_error or "run failed"
            else:
                payload["type"] = "state"
            yield f"data: {json.dumps(payload, default=str, ensure_ascii=False)}\n\n"

        return StreamingResponse(history_stream(), media_type="text/event-stream")

    @app.get("/tape")
    async def tape_page() -> HTMLResponse:
        path = _STATIC_DIR / "index.html"
        html = await anyio.to_thread.run_sync(path.read_text, "utf-8")
        return HTMLResponse(content=html, headers={"Cache-Control": "no-cache"})

    @app.get("/")
    async def index() -> HTMLResponse:
        path = _STATIC_DIR / "index.html"
        html = await anyio.to_thread.run_sync(path.read_text, "utf-8")
        return HTMLResponse(
            content=html,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    return app


def _stream_from_ctx(ctx: RunContext) -> StreamingResponse:
    async def event_stream() -> Any:
        ctx.streamers += 1
        try:
            while True:
                try:
                    event = await asyncio.wait_for(ctx.queue.get(), timeout=_SSE_HEARTBEAT_S)
                except TimeoutError:
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
                    continue
                yield f"data: {json.dumps(event, default=str, ensure_ascii=False)}\n\n"
                if event.get("type") in ("failed", "completed"):
                    return
        finally:
            ctx.streamers -= 1
            if ctx.streamers <= 0 and ctx.done:
                ctx.terminal_delivered.set()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def main(argv: list[str] | None = None) -> int:
    """Serve the playground — the ``prodagent`` console script entry point."""
    import os

    # The playground terminal mirrors agent activity next to the browser UI.
    os.environ.setdefault("PRODAGENT_CONSOLE", "1")

    parser = argparse.ArgumentParser(
        prog="prodagent",
        description="Serve the interactive prodagent playground.",
    )
    parser.add_argument("--port", type=int, default=8765, help="Port to serve on.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open a browser.")
    args = parser.parse_args(argv)

    if not args.no_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(f"http://{args.host}:{args.port}")).start()
    print(f"prodagent playground — http://{args.host}:{args.port}")

    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(build_app(), host=args.host, port=args.port, log_level="info")
    )
    try:
        asyncio.run(server.serve())
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        logger.exception("prodagent playground failed")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_app", "main"]
