from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal

TransportKind = Literal["stdio", "http"]


def expand_env(value: str) -> str:
    """Expand ``${VAR}`` / ``$VAR`` against os.environ; unset vars left as-is."""
    return os.path.expandvars(value)


def _expand_value(value: Any) -> Any:
    if isinstance(value, str):
        return expand_env(value)
    if isinstance(value, list):
        return [_expand_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_value(v) for k, v in value.items()}
    return value


@dataclass
class MCPServerConfig:
    name: str
    transport: TransportKind = "stdio"
    # stdio
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # http
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    timeout_ms: int = 300_000
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.transport == "stdio":
            if not self.command:
                raise ValueError(f"MCP server {self.name!r}: stdio transport requires a 'command'")
        else:
            if not self.url:
                raise ValueError(
                    f"MCP server {self.name!r}: {self.transport} transport requires a 'url'"
                )
        if self.timeout_ms < 1000:
            raise ValueError(
                f"MCP server {self.name!r}: timeout_ms must be >= 1000, got {self.timeout_ms}"
            )

    @property
    def call_timeout(self) -> float:
        return self.timeout_ms / 1000.0

    @classmethod
    def from_dict(cls, name: str, entry: dict[str, Any]) -> MCPServerConfig:
        raw = {k: _expand_value(v) for k, v in entry.items()}

        raw_type = raw.pop("type", None)
        if raw_type in ("streamable-http", "sse"):
            raw_type = "http"
        if raw_type is None:
            if "url" in raw and raw.get("url"):
                raise ValueError(
                    f"MCP server {name!r}: entry has 'url' but no 'type' — set type to 'http'"
                )
            transport: TransportKind = "stdio"
        elif raw_type in ("stdio", "http"):
            transport = raw_type
        else:
            raise ValueError(
                f"MCP server {name!r}: unknown transport type {raw_type!r} "
                "(expected 'stdio' or 'http')"
            )

        timeout_raw = raw.pop("timeout", None)
        timeout_ms = int(float(timeout_raw)) if timeout_raw is not None else 300_000
        enabled = bool(raw.pop("enabled", True))

        return cls(
            name=name,
            transport=transport,
            command=str(raw.get("command", "")),
            args=list(raw.get("args", []) or []),
            env=dict(raw.get("env", {}) or {}),
            url=str(raw.get("url", "")),
            headers=dict(raw.get("headers", {}) or {}),
            timeout_ms=timeout_ms,
            enabled=enabled,
        )


def load_mcp_servers(spec: dict[str, Any] | str) -> list[MCPServerConfig]:
    if isinstance(spec, str):
        import json

        text = _read_file(spec)
        if spec.endswith((".yaml", ".yml")):
            try:
                import yaml
            except ImportError as exc:
                raise RuntimeError(
                    "Loading MCP config from YAML requires pyyaml: pip install pyyaml"
                ) from exc
            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
    else:
        data = spec

    servers_map = data.get("mcpServers", data) if isinstance(data, dict) else {}
    if not isinstance(servers_map, dict):
        raise ValueError("MCP config must be a {name: entry} mapping under 'mcpServers'")

    configs: list[MCPServerConfig] = []
    for srv_name, entry in servers_map.items():
        if not isinstance(entry, dict):
            raise ValueError(f"MCP server {srv_name!r}: entry must be a dict, got {type(entry)}")
        cfg = MCPServerConfig.from_dict(srv_name, entry)
        if cfg.enabled:
            configs.append(cfg)
    return configs


def _read_file(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()
