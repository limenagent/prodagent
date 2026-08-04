"""File-backed span exporters — JSONL trace file + Python logging sink."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from prodagent.resilience.observability.audit import AgentSpan

logger = logging.getLogger(__name__)

__all__ = ["FileSpanExporter", "LogExporter"]


class LogExporter:
    """Structured JSON to the Python logging system. Zero dependencies."""

    def export(self, span: AgentSpan) -> None:
        if span.error:
            logger.error("AUDIT %s", span.to_log_line())
        else:
            logger.info("AUDIT %s", span.to_log_line())

    def shutdown(self) -> None:
        pass


class FileSpanExporter:
    """Append-only JSONL trace file — the eval/replay baseline."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._f: Any = None
        self._lock = threading.Lock()
        self._closed: bool = False

    def _ensure_open(self) -> None:
        if self._f is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._f = self._path.open("a", encoding="utf-8")

    def export(self, span: AgentSpan) -> None:
        with self._lock:
            if self._closed:
                logger.error("FileSpanExporter write after shutdown for %s", self._path)
                return
            try:
                self._ensure_open()
                self._f.write(json.dumps(span.to_dict(), default=str, ensure_ascii=False) + "\n")
                self._f.flush()
            except OSError as exc:
                logger.error("FileSpanExporter write failed for %s: %s", self._path, exc)

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._f is None:
                return
            try:
                self._f.flush()
                self._f.close()
            except (OSError, ValueError):
                pass
