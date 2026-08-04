"""Confidence × reversibility routing matrix."""

from __future__ import annotations

from typing import TYPE_CHECKING

from prodagent.core.types import SideEffectLevel
from prodagent.ports.approval import ApprovalDecision

if TYPE_CHECKING:
    from prodagent.core.types import ToolCall, ToolMeta

_HIGH_CONFIDENCE = 0.85
_HIGH_REVERSIBILITY = 0.70


def extract_confidence(call: ToolCall) -> float | None:
    meta = call.metadata or {}
    if "confidence" not in meta:
        return None
    raw = meta["confidence"]
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _route(confidence: float, reversibility: float) -> ApprovalDecision:
    high_conf = confidence >= _HIGH_CONFIDENCE
    high_rev = reversibility >= _HIGH_REVERSIBILITY

    if high_conf and high_rev:
        return ApprovalDecision.AUTO_EXECUTE
    if high_conf and not high_rev:
        return ApprovalDecision.BRIEF_APPROVAL
    if not high_conf and high_rev:
        return ApprovalDecision.AUTO_EXECUTE
    return ApprovalDecision.FULL_APPROVAL


def should_request_review(
    meta: ToolMeta | None,
    confidence: float | None,
) -> ApprovalDecision:
    if confidence is None:
        return ApprovalDecision.FULL_APPROVAL
    if meta is None:
        reversibility: float = 0.5
    else:
        raw_reversibility = meta.reversibility
        if raw_reversibility is None and meta.side_effect_level in (
            SideEffectLevel.MEDIUM,
            SideEffectLevel.HIGH,
        ):
            raise ValueError(
                f"Tool {meta.name!r} declares side_effect_level={meta.side_effect_level.value} "
                "but reversibility is None — declare an explicit reversibility value "
                "(0.0 = irreversible, 1.0 = fully reversible)."
            )
        reversibility = raw_reversibility if raw_reversibility is not None else 0.9

    return _route(confidence, reversibility)
