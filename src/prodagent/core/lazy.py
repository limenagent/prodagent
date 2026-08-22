"""One lazy-package pattern for every ``__init__`` that must stay import-light.

Usage::

    from prodagent.core.lazy import lazy_package

    _SYMBOL_SOURCES = {"Agent": "prodagent.runtime.agent", ...}

    __getattr__, __dir__ = lazy_package(_SYMBOL_SOURCES)

``__all__`` derives from the source map. Symbols defined in the init itself
stay in ``globals()`` and are found before ``__getattr__`` runs.
"""

from __future__ import annotations

from typing import Any


def lazy_package(sources: dict[str, str]) -> tuple[Any, Any]:
    import importlib

    def __getattr__(name: str) -> Any:
        source = sources.get(name)
        if source is None:
            raise AttributeError(f"module has no attribute {name!r}")
        module = importlib.import_module(source)
        try:
            value = getattr(module, name)
        except AttributeError:
            raise AttributeError(f"module {source!r} has no attribute {name!r}") from None
        globals()[name] = value
        return value

    def __dir__() -> list[str]:
        return sorted(sources)

    return __getattr__, __dir__
