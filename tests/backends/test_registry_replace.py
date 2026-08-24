"""A replaced FrameworkConfig keeps its BackendRegistry (shared pools)."""

from __future__ import annotations

import dataclasses

from prodagent.backends.registry import BackendRegistry
from prodagent.core.config import FrameworkConfig


def test_replace_preserves_backend_registry() -> None:
    fw = FrameworkConfig.default()
    reg = BackendRegistry.for_config(fw)
    fw2 = dataclasses.replace(fw, profile="production")
    assert fw2._backend_registry is reg
