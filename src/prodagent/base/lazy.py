"""One lazy-package pattern for every ``__init__`` that must stay import-light.

The top-level package exports dozens of symbols but refuses to pay for
loading them all up front: PEP 562 module ``__getattr__`` resolves each
symbol only on first access, then caches it in ``globals()`` — a closed-
stack library: the catalogue is always open, the book is fetched on request.

Usage::

    from prodagent.base.lazy import lazy_package

    _SYMBOL_SOURCES = {"Agent": "prodagent.runtime.agent", ...}

    __getattr__, __dir__ = lazy_package(_SYMBOL_SOURCES)

``__all__`` derives from the source map. Symbols defined in the init itself
stay in ``globals()`` and are found before ``__getattr__`` runs.
"""

from __future__ import annotations

from typing import Any


def lazy_package(sources: dict[str, str]) -> tuple[Any, Any]:
    """Build the (``__getattr__``, ``__dir__``) pair for a lazy package init."""
    import importlib

    def __getattr__(name: str) -> Any:
        source = sources.get(name)
        if source is None:
            raise AttributeError(
                f"module has no attribute {name!r}"
            )  # not lazily mapped — genuinely absent
        module = importlib.import_module(source)  # first touch: the only expensive step
        try:
            value = getattr(module, name)
        except AttributeError:
            # The map lied about where the symbol lives — fail with the
            # map's own answer so the fix is obvious.
            raise AttributeError(f"module {source!r} has no attribute {name!r}") from None
        globals()[name] = value  # cache: subsequent lookups skip __getattr__ entirely
        return value

    def __dir__() -> list[str]:
        return sorted(sources)

    return __getattr__, __dir__
