"""Postgres-backed ``SpanExporter``."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

    from prodagent.core.observability import AgentSpan

__all__ = ["PostgresSpanExporter"]


class PostgresSpanExporter:
    """Append-only span sink backed by a Postgres table."""

    def __init__(self, pool: ConnectionPool, *, namespace: str = "default") -> None:
        self._pool = pool
        self._ns = namespace
        self._closed = False

    def export(self, span: AgentSpan) -> None:
        if self._closed:
            return
        blob = json.dumps(asdict(span), default=str, ensure_ascii=False)
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO pa_span (namespace, payload) VALUES (%s, %s::jsonb)",
                    (self._ns, blob),
                )
            conn.commit()

    def shutdown(self) -> None:
        self._closed = True
