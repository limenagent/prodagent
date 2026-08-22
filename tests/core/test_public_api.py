import ast
import importlib.metadata
import sys
from pathlib import Path

SRC_DIR = Path(__file__).parent.parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def test_all_symbols_are_importable():
    import prodagent

    for name in prodagent.__all__:
        assert hasattr(prodagent, name), f"{name!r} in __all__ but not importable"


def test_all_are_actual_imports():
    import prodagent

    for name in prodagent.__all__:
        value = getattr(prodagent, name)
        if name == "__version__":
            continue
        assert value is not None, f"{name!r} in __all__ but is None"
        assert not isinstance(value, str) or name == "__version__", (
            f"{name!r} in __all__ but appears to be a string literal, not an import"
        )


def test_no_duplicate_all_entries():
    import prodagent

    seen = set()
    duplicates = []
    for name in prodagent.__all__:
        if name in seen:
            duplicates.append(name)
        seen.add(name)

    assert len(duplicates) == 0, f"__all__ contains duplicates: {duplicates}"


def test_all_imports_declared_in_all():
    import prodagent

    public_set = set(prodagent.__all__)

    symbol_sources = getattr(prodagent, "_SYMBOL_SOURCES", {})
    undeclared = set(symbol_sources) - public_set
    assert not undeclared, (
        f"_SYMBOL_SOURCES has entries not in __all__:\n{sorted(undeclared)}\n"
        f"Add them to __all__ or remove from _SYMBOL_SOURCES."
    )

    init_file = Path(prodagent.__file__).resolve()
    source = init_file.read_text()
    tree = ast.parse(source)

    def _is_type_checking_if(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        )

    # Skip `if TYPE_CHECKING:` blocks — those are lazy by design (never run at
    # runtime), so they can't defeat lazy loading.
    for stmt in tree.body:
        if _is_type_checking_if(stmt):
            continue
        for node in ast.walk(stmt):
            # Machinery imports are exempt: core.lazy IS the lazy loader;
            # ports.llm is the contract the llm package re-exports eagerly.
            _MACHINERY = ("prodagent.core.lazy", "prodagent.ports.llm")
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("prodagent.")
                and node.module not in _MACHINERY
            ):
                for alias in node.names:
                    raise AssertionError(
                        f"Eager top-level import in __init__.py defeats lazy "
                        f"loading: `from {node.module} import {alias.name}`. "
                        f"Move to _SYMBOL_SOURCES."
                    )


def test_version_is_available():
    import prodagent

    assert "__version__" in prodagent.__all__
    assert hasattr(prodagent, "__version__")
    version = prodagent.__version__
    assert isinstance(version, str)
    assert len(version) > 0
    if not version.startswith("0.0.0-dev"):
        parts = version.split(".")
        assert len(parts) >= 3, f"Version {version!r} doesn't look like semver"


def test_core_symbols_available():
    import prodagent

    assert "Agent" in prodagent.__all__
    assert "tool" in prodagent.__all__

    from prodagent import Agent, tool

    assert Agent is not None
    assert tool is not None


def test_package_name_metadata():
    try:
        version = importlib.metadata.version("prodagent")
        assert isinstance(version, str)
        assert len(version) > 0
    except importlib.metadata.PackageNotFoundError:
        pass
