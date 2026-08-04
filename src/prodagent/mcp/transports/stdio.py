from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from typing import Any

from prodagent.mcp.transports._rpc import RPCError

logger = logging.getLogger(__name__)

_STDERR_PIPE_CAPACITY = 64 * 1024


def _rpc_request(method: str, params: dict[str, Any] | None, req_id: int | str) -> bytes:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return (json.dumps(msg) + "\n").encode()


def _rpc_notification(method: str, params: dict[str, Any] | None = None) -> bytes:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params:
        msg["params"] = params
    return (json.dumps(msg) + "\n").encode()


class StdioTransport:
    def __init__(self, *, read_timeout: float = 30.0) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._write_lock = asyncio.Lock()
        self._req_id: int = 0
        self._read_timeout = read_timeout
        self._stderr_drainer: asyncio.Task[None] | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}

    async def connect(
        self,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        full_env = {**os.environ, **(env or {})}
        self._proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=full_env,
            start_new_session=True,
        )
        self._stderr_drainer = asyncio.create_task(self._drain_stderr())
        self._reader = asyncio.create_task(self._read_stdout())
        logger.info("MCP stdio transport: spawned %s (pid=%d)", command[0], self._proc.pid)

    async def _drain_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    return
                logger.debug(
                    "MCP stdio stderr[%d]: %s",
                    self._proc.pid if self._proc else -1,
                    line.decode(errors="replace").rstrip(),
                )
        except Exception as exc:
            logger.debug("MCP stdio stderr drainer ended: %s", exc)

    async def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                raw_line = await proc.stdout.readline()
                if not raw_line:
                    break
                try:
                    msg = json.loads(raw_line)
                except json.JSONDecodeError:
                    logger.debug("MCP stdio: skipping non-JSON line: %r", raw_line[:120])
                    continue
                req_id = msg.get("id")
                if req_id is None:
                    # Server notification — no future to resolve.
                    continue
                future = self._pending.get(req_id)
                if future is None or future.done():
                    continue
                if "error" in msg:
                    err = msg["error"]
                    future.set_exception(RPCError(err.get("code", -1), err.get("message", "")))
                else:
                    future.set_result(msg.get("result", {}))
        except Exception as exc:
            self._fail_all(exc)
            return
        self._fail_all(EOFError("MCP server closed stdout unexpectedly"))

    def _fail_all(self, exc: BaseException) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._proc is None or self._reader is None:
            raise RuntimeError("Not connected — call connect() first")
        self._req_id += 1
        req_id = self._req_id
        payload = _rpc_request(method, params, req_id)

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[req_id] = future

        try:
            async with self._write_lock:
                assert self._proc is not None and self._proc.stdin is not None
                self._proc.stdin.write(payload)
                await self._proc.stdin.drain()
        except Exception:
            self._pending.pop(req_id, None)
            raise

        try:
            return await asyncio.wait_for(future, timeout=self._read_timeout)
        except TimeoutError:
            self._pending.pop(req_id, None)
            raise EOFError(
                f"MCP stdio: no response for id={req_id} within {self._read_timeout}s"
            ) from None

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self._proc is None:
            return
        async with self._write_lock:
            assert self._proc.stdin is not None
            self._proc.stdin.write(_rpc_notification(method, params))
            await self._proc.stdin.drain()

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
            self._reader = None

        if self._stderr_drainer is not None:
            self._stderr_drainer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_drainer
            self._stderr_drainer = None

        self._fail_all(asyncio.CancelledError("transport closing"))

        proc = self._proc
        if proc and proc.returncode is None:
            _kill_process_group(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()  # reap the zombie
        if proc is not None:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    with contextlib.suppress(Exception):
                        stream.close()  # type: ignore[union-attr]
        self._proc = None


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    if proc.pid is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
