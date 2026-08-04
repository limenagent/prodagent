"""Security circuit breaker — suspend agents after repeated violations."""

from __future__ import annotations

import logging

from prodagent.core.exceptions import AgentSuspended

logger = logging.getLogger(__name__)


class PermissionCircuitBreaker:
    def __init__(self, failure_threshold: int = 3) -> None:
        self._threshold = failure_threshold
        self._counts: dict[str, int] = {}
        self._suspended: set[str] = set()

    def check(self, agent_id: str) -> None:
        if agent_id in self._suspended:
            raise AgentSuspended(
                f"Agent '{agent_id}' is suspended by the security circuit breaker. "
                "Manual operator reset required.",
                agent_id=agent_id,
            )

    def record_violation(self, agent_id: str, reason: str = "") -> None:
        count = self._counts.get(agent_id, 0) + 1
        self._counts[agent_id] = count
        logger.warning(
            "Security violation #%d for agent '%s'%s",
            count,
            agent_id,
            f": {reason}" if reason else "",
        )

        if count >= self._threshold:
            self._suspended.add(agent_id)
            logger.error(
                "SECURITY CIRCUIT OPEN: agent '%s' suspended after %d violations",
                agent_id,
                count,
            )
            raise AgentSuspended(
                f"Agent '{agent_id}' suspended after {count} security violations. "
                "Manual operator reset required.",
                agent_id=agent_id,
                violation_count=count,
            )

    def reset(self, agent_id: str) -> None:
        self._suspended.discard(agent_id)
        self._counts.pop(agent_id, None)
        logger.info("Security circuit reset by operator for agent '%s'", agent_id)
