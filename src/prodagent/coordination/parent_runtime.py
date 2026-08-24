"""Shim — fork machinery moved to :mod:`prodagent.runtime.parent_runtime`."""

from prodagent.runtime.parent_runtime import (  # noqa: F401
    ParentRuntime,
    SpawnAccumulator,
    describe_agent,
    fold_spawn_fields,
)

__all__ = ["ParentRuntime", "SpawnAccumulator", "describe_agent", "fold_spawn_fields"]
