"""MCP layer 2 — the transport seam: "how to connect" is a port here too.

The client speaks connect/send/notify/close and never learns whether the
server is a subprocess or a URL; a new transport (websocket, ...) is a new
implementation of this protocol, nothing else moves."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Transport(Protocol):
    """The client-facing surface every transport satisfies."""

    async def connect(self, *args: Any, **kwargs: Any) -> None: ...

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None: ...

    async def close(self) -> None: ...
