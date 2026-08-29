"""JSON-RPC error shape — the one piece both transports share."""

from __future__ import annotations


class RPCError(Exception):
    """A JSON-RPC error response from an MCP server. Carries the protocol
    ``code`` — the bridge maps codes onto retry policy (method/params
    errors are permanent; the rest are worth backing off and retrying)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"MCP RPC error {code}: {message}")
        self.code = code
