from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from prodagent.mcp.transports._rpc import RPCError

logger = logging.getLogger(__name__)


class HttpTransport:
    def __init__(self, *, timeout: float = 30.0) -> None:
        self._url: str = ""
        self._headers: dict[str, str] = {}
        self._session: Any = None  # httpx.AsyncClient
        self._timeout = timeout
        self._req_id: int = 0

    async def connect(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError(
                "httpx is required for MCP HTTP transport: pip install httpx"
            ) from exc
        self._url = url
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **(headers or {}),
        }
        self._session = httpx.AsyncClient(
            headers=self._headers,
            timeout=self._timeout,
        )
        logger.info("MCP HTTP transport: connected to %s", url)

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("Not connected — call connect() first")
        self._req_id += 1
        req_id = self._req_id

        body: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            body["params"] = params

        try:
            resp = await self._session.post(self._url, json=body)
            resp.raise_for_status()
        except Exception as exc:
            status = _extract_status(exc)
            if status is not None and 400 <= status < 500:
                raise RPCError(
                    -32602,
                    f"HTTP {status} from MCP server (client error): {exc}",
                ) from exc
            if status is not None and 500 <= status < 600:
                raise RPCError(
                    -32000,
                    f"HTTP {status} from MCP server (server error): {exc}",
                ) from exc
            raise

        ctype = resp.headers.get("content-type", "").lower()
        if "text/event-stream" in ctype:
            return await _read_sse_response(resp, req_id, timeout=self._timeout)
        return _extract_result(resp.json(), req_id)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self._session is None:
            return
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            body["params"] = params
        await self._session.post(self._url, json=body)

    async def close(self) -> None:
        if self._session:
            await self._session.aclose()
            self._session = None


def _extract_status(exc: Exception) -> int | None:
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    return getattr(resp, "status_code", None)


async def _read_sse_response(resp: Any, req_id: int | str, *, timeout: float) -> dict[str, Any]:
    async def _collect() -> dict[str, Any]:
        async for line in resp.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if not payload:
                continue
            try:
                msg = json.loads(payload)
            except json.JSONDecodeError:
                logger.debug("SSE: skipping unparseable line: %r", payload[:120])
                continue
            if msg.get("id") != req_id:
                # Other reqs' notifications / server-initiated messages — ignore.
                continue
            return _extract_result(msg, req_id)
        raise EOFError(f"SSE stream closed before response for id={req_id} arrived")

    try:
        return await asyncio.wait_for(_collect(), timeout=timeout)
    except TimeoutError as exc:
        raise EOFError(f"SSE stream timed out after {timeout}s waiting for id={req_id}") from exc


def _extract_result(msg: dict[str, Any], req_id: int | str) -> dict[str, Any]:
    if "error" in msg:
        err = msg["error"]
        raise RPCError(err.get("code", -1), err.get("message", ""))
    result = msg.get("result", {})
    return result if isinstance(result, dict) else {"result": result}
