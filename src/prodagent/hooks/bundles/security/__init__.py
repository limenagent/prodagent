"""Security hook bundles."""

from prodagent.hooks.bundles.security.approval import ApprovalHooks
from prodagent.hooks.bundles.security.injection import InjectionDefenseHooks

__all__ = [
    "ApprovalHooks",
    "InjectionDefenseHooks",
]
