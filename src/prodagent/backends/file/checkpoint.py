"""File-based checkpoint store — versioned JSON snapshots."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from prodagent.backends.file._locking import _exclusive
from prodagent.base.errors import CorruptedCheckpointError, VersionConflict
from prodagent.base.io import safe_filename_component, write_atomic_json
from prodagent.kernel.state import AgentRun

logger = logging.getLogger(__name__)

__all__ = ["FileCheckpointStore"]

SCHEMA_VERSION = 1
_VERSION_RE = re.compile(r"\.v(\d+)\.json$")


class FileCheckpointStore:
    """Versioned JSON checkpoint store backed by the local filesystem."""

    def __init__(self, directory: str | Path = ".prodagent/runs", fsync: bool = False) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._fsync = fsync

    def _base(self, run_id: str) -> str:
        return safe_filename_component(run_id)

    def _path(self, run_id: str, version: int) -> Path:
        return self._dir / f"{self._base(run_id)}.v{version}.json"

    def _lock_path(self, run_id: str) -> Path:
        return self._dir / f"{self._base(run_id)}.lock"

    def _latest_version(self, run_id: str) -> int:
        best = 0
        for p in self._dir.glob(f"{self._base(run_id)}.v*.json"):
            m = _VERSION_RE.search(p.name)
            if m:
                best = max(best, int(m.group(1)))
        return best

    async def save(self, run: AgentRun, expected_version: int | None = None) -> None:
        await asyncio.to_thread(self._save_sync, run, expected_version)

    def _save_sync(self, run: AgentRun, expected_version: int | None) -> None:
        try:
            with _exclusive(self._lock_path(run.run_id)):
                current_version = self._latest_version(run.run_id)

                if expected_version is not None and expected_version != current_version:
                    raise VersionConflict(
                        f"checkpoint version mismatch for run={run.run_id}: "
                        f"expected {expected_version}, stored {current_version}. "
                        f"A concurrent writer persisted a newer snapshot — reload and rebase."
                    )

                new_version = current_version + 1
                target = self._path(run.run_id, new_version)
                envelope: dict[str, Any] = {
                    "schema_version": SCHEMA_VERSION,
                    "version": new_version,
                    "run": run.to_dict(),
                }
                write_atomic_json(target, envelope, fsync=self._fsync)
                run.checkpoint_version = new_version
                logger.debug(
                    "[checkpoint] saved run=%s version=%d turns=%d",
                    run.run_id,
                    new_version,
                    run.turn_count,
                )
        except OSError as exc:
            run.checkpoint_failed = True
            logger.error(
                "[checkpoint] save failed for run=%s (checkpoint_failed=True): %s",
                run.run_id,
                exc,
            )

    async def load(self, run_id: str, version: int | None = None) -> AgentRun | None:
        return await asyncio.to_thread(self._load_sync, run_id, version)

    def _load_sync(self, run_id: str, version: int | None = None) -> AgentRun | None:
        if version is None:
            version = self._latest_version(run_id)
            if version == 0:
                return None
        path = self._path(run_id, version)
        if not path.exists():
            return None
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            stored_schema = envelope.get("schema_version", 0)
            if stored_schema != SCHEMA_VERSION:
                raise CorruptedCheckpointError(
                    f"checkpoint for run={run_id} has schema_version "
                    f"{stored_schema}, expected {SCHEMA_VERSION}. "
                    f"The on-disk format is incompatible — inspect or delete manually."
                )
            run_payload = envelope["run"]
            stored_version = int(envelope.get("version", version))
            run = AgentRun.from_dict(run_payload)
            run.checkpoint_version = stored_version
        except json.JSONDecodeError as exc:
            raise CorruptedCheckpointError(
                f"checkpoint for run={run_id} v{version} is not valid JSON: {exc}"
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise CorruptedCheckpointError(
                f"checkpoint for run={run_id} v{version} failed schema validation: {exc}"
            ) from exc
        except OSError as exc:
            raise CorruptedCheckpointError(
                f"checkpoint for run={run_id} v{version} could not be read: {exc}"
            ) from exc
        logger.info(
            "[checkpoint] loaded run=%s version=%d turns=%d",
            run_id,
            stored_version,
            run.turn_count,
        )
        return run

    async def list_versions(self, run_id: str) -> list[int]:
        return await asyncio.to_thread(self._list_versions_sync, run_id)

    def _list_versions_sync(self, run_id: str) -> list[int]:
        versions: list[int] = []
        for p in self._dir.glob(f"{self._base(run_id)}.v*.json"):
            m = _VERSION_RE.search(p.name)
            if m:
                versions.append(int(m.group(1)))
        return sorted(versions)

    async def fork(
        self,
        run_id: str,
        at_version: int,
        new_run_id: str | None = None,
    ) -> str:
        """Create a new run from a historical snapshot and return its id."""
        return await asyncio.to_thread(self._fork_sync, run_id, at_version, new_run_id)

    def _fork_sync(self, run_id: str, at_version: int, new_run_id: str | None) -> str:
        source = self._load_sync(run_id, at_version)
        if source is None:
            raise CorruptedCheckpointError(
                f"cannot fork: no checkpoint for run={run_id} at version={at_version}"
            )
        forked_id = new_run_id or f"{run_id}:fork-v{at_version}-{uuid.uuid4().hex[:8]}"
        source.run_id = forked_id
        source.plan_last_seq = 0
        source.checkpoint_version = 0

        with _exclusive(self._lock_path(forked_id)):
            if self._latest_version(forked_id) != 0:
                raise VersionConflict(
                    f"fork target run_id={forked_id} already has checkpoints — "
                    "pass a fresh new_run_id."
                )
            target = self._path(forked_id, 1)
            envelope: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "version": 1,
                "run": source.to_dict(),
            }
            write_atomic_json(target, envelope, fsync=self._fsync)
            source.checkpoint_version = 1
        logger.info(
            "[checkpoint] forked run=%s v%d → %s (plan_last_seq reset to 0)",
            run_id,
            at_version,
            forked_id,
        )
        return forked_id

    async def list_run_ids(self) -> list[str]:
        return await asyncio.to_thread(self._list_run_ids_sync)

    def _list_run_ids_sync(self) -> list[str]:
        seen: set[str] = set()
        for p in self._dir.glob("*.v*.json"):
            m = _VERSION_RE.search(p.name)
            if m:
                seen.add(p.name[: m.start()])
        return sorted(seen)
