"""Playground-only: resolve a chain's parked peer run id (peer resume)."""

from __future__ import annotations

from typing import Any


async def resolve_suspended_peer_run_id(agent: Any, root_run_id: str) -> str | None:
    """Where a parked chain continues: the pending handoff names the next
    peer and its run id. Returns None for a fresh (non-parked) run."""
    from prodagent.runtime.compose import find_suspended_peer

    parked = await find_suspended_peer(agent.checkpoint, root_run_id)
    return parked[1] if parked else None
