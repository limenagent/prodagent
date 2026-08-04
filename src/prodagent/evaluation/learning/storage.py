"""Append-only JSONL journal of completed agent runs."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from prodagent.core.io import read_jsonl
from prodagent.evaluation.learning.experience import ExperienceRecord

logger = logging.getLogger(__name__)

# Rolling store: trim JSONL to this many records after each write.
_DEFAULT_MAX_RECORDS = 500

__all__ = ["FileExperienceStore"]


class FileExperienceStore:
    """Append-only JSONL store for agent run experiences.

    Writes are synchronous — callers already run this in a background
    ``asyncio.create_task`` (LearningHooks._safely_run_loop), so a microsecond
    disk append doesn't block the agent loop. A file lock guards against
    concurrent writers within the same process.
    """

    def __init__(self, path: str | Path, *, max_records: int = _DEFAULT_MAX_RECORDS) -> None:
        self._path = Path(path)
        self._max_records = max_records
        self._file_lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.touch()

    def record(self, record: ExperienceRecord) -> None:
        """Append *record* to the journal."""
        line = record.to_jsonl()
        try:
            with self._file_lock:
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
                self._trim_if_needed()
        except Exception:
            logger.exception("FileExperienceStore: failed to write record")

    def _trim_if_needed(self) -> None:
        """Keep only the most recent max_records lines; rewrite if over limit.

        Caller must hold ``self._file_lock``.
        """
        try:
            text = self._path.read_text(encoding="utf-8")
            lines = [ln for ln in text.splitlines() if ln.strip()]
            if len(lines) > self._max_records:
                keep = lines[-self._max_records :]
                tmp = self._path.with_suffix(".tmp")
                tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
                tmp.replace(self._path)
        except Exception:
            logger.debug("FileExperienceStore: trim failed")

    def load_all(self) -> list[ExperienceRecord]:
        """Load every record from the journal (most recent last)."""
        records: list[ExperienceRecord] = []
        try:
            with self._file_lock:
                for d in read_jsonl(self._path):
                    try:
                        records.append(ExperienceRecord.from_dict(d))
                    except Exception:
                        logger.debug("FileExperienceStore: skipping malformed line")
        except FileNotFoundError:
            pass
        return records
