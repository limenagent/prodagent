from __future__ import annotations


class RPCError(Exception):
    """A JSON-RPC error response from an MCP server."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"MCP RPC error {code}: {message}")
        self.code = code
