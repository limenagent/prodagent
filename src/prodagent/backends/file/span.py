"""File-backed span exporter — append-only JSONL trace file."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from prodagent.core.observability import AgentSpan

logger = logging.getLogger(__name__)

__all__ = ["FileSpanExporter"]


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

    async def export(self, span: AgentSpan) -> None:
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

    async def shutdown(self) -> None:
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
