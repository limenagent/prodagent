"""Backend implementations for ``prodagent.ports``.

Layout (by backend, not by port):
  memory/      — in-process, ephemeral (default for ephemeral state)
  file/        — single-host, file-backed (default for durable state)
  redis/       — distributed ephemeral, [redis] extra
  postgres/    — distributed durable, [postgres] extra
  neo4j/       — graph database, [neo4j] extra
  factory.py   — ``resolve_*()``: pick a backend per port from FrameworkConfig
  registry.py  — per-config lazy cache of shared Redis/Postgres clients

Importing ``prodagent.backends`` does not pull in any third-party deps;
backends are lazy-imported by their loader.
"""
