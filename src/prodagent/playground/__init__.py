"""Interactive web playground — trigger example agents from a browser."""

from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv() -> None:
    """Load .env from CWD at import time so create_llm_client() sees the vars."""
    p = Path.cwd() / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

from prodagent.playground.registry import (  # noqa: E402
    ExampleSpec,
    RunRegistry,
    discover_examples,
)
from prodagent.playground.web_hooks import WebPushHooks  # noqa: E402

__all__ = ["ExampleSpec", "RunRegistry", "WebPushHooks", "discover_examples"]
