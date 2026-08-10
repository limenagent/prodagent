"""Redis-backed ``ApprovalStore``."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from prodagent.backends.redis.keys import namespaced_key
from prodagent.ports.approval import ApprovalDecision, ApprovalRequest

if TYPE_CHECKING:
    from redis.asyncio import Redis

__all__ = ["RedisApprovalStore"]


def _req_to_dict(req: ApprovalRequest) -> dict[str, Any]:
    return {
        "request_id": req.request_id,
        "tool_name": req.tool_name,
        "params": req.params,
        "confidence": req.confidence,
        "reversibility": req.reversibility,
        "context_summary": req.context_summary,
        "run_id": req.run_id,
        "created_at": req.created_at,
        "decision": req.decision.value if req.decision else None,
        "decided_at": req.decided_at,
        "approver_id": req.approver_id,
    }


def _req_from_dict(d: dict[str, Any]) -> ApprovalRequest:
    decision_raw = d.get("decision")
    return ApprovalRequest(
        request_id=d["request_id"],
        tool_name=d.get("tool_name", ""),
        params=d.get("params", {}),
        confidence=d.get("confidence", 0.0),
        reversibility=d.get("reversibility", 0.5),
        context_summary=d.get("context_summary", ""),
        run_id=d.get("run_id", ""),
        created_at=d.get("created_at", time.time()),
        decision=ApprovalDecision(decision_raw) if decision_raw else None,
        decided_at=d.get("decided_at"),
        approver_id=d.get("approver_id"),
    )


class RedisApprovalStore:
    """Durable, multi-replica ``ApprovalStore``."""

    def __init__(self, client: Redis, *, namespace: str = "default") -> None:
        self._client = client
        self._ns = namespace

    def _key(self, request_id: str) -> str:
        return namespaced_key(self._ns, "approval", request_id)

    async def create_request(self, req: ApprovalRequest) -> None:
        key = self._key(req.request_id)
        await self._client.set(key, json.dumps(_req_to_dict(req), ensure_ascii=False), nx=True)

    async def get_request(self, request_id: str) -> ApprovalRequest | None:
        blob = await self._client.get(self._key(request_id))
        if blob is None:
            return None
        if isinstance(blob, bytes):
            blob = blob.decode()
        return _req_from_dict(json.loads(blob))

    async def submit_decision(
        self,
        request_id: str,
        decision: ApprovalDecision,
        approver_id: str = "",
    ) -> None:
        key = self._key(request_id)
        decided_at = time.time()
        from redis.exceptions import WatchError

        while True:
            async with self._client.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    blob = await pipe.get(key)
                    if blob is None:
                        req = ApprovalRequest(
                            request_id=request_id,
                            tool_name="",
                            params={},
                            confidence=0.0,
                            reversibility=0.5,
                            context_summary="",
                        )
                    else:
                        if isinstance(blob, bytes):
                            blob = blob.decode()
                        req = _req_from_dict(json.loads(blob))
                    req.decision = decision
                    req.approver_id = approver_id
                    req.decided_at = decided_at
                    pipe.multi()
                    pipe.set(key, json.dumps(_req_to_dict(req), ensure_ascii=False))
                    await pipe.execute()
                    return
                except WatchError:
                    continue
