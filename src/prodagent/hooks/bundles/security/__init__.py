"""Security hook bundles."""

from prodagent.hooks.bundles.security.approval import ApprovalHooks
from prodagent.hooks.bundles.security.injection import InjectionDefenseHooks
from prodagent.hooks.bundles.security.permission import PermissionHooks

__all__ = [
    "PermissionHooks",
    "ApprovalHooks",
    "InjectionDefenseHooks",
]
