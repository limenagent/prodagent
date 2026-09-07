"""Purity guard: the kernel may depend only on the Python stdlib and src itself.

This test turns "mechanism inside, policy outside, the kernel knows no vendor SDK"
into an executable constraint: import openai/httpx/any third-party lib in kernel
and CI turns red immediately.
"""

import ast
import pathlib
import sys

KERNEL_DIR = pathlib.Path(__file__).resolve().parent.parent / "src" / "kernel"
STDLIB = set(sys.stdlib_module_names)


def imported_roots(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_kernel_has_no_third_party_imports():
    offenders = {}
    for path in KERNEL_DIR.glob("*.py"):
        if path.name == "__init__.py":
            continue
        third_party = {r for r in imported_roots(path) if r not in STDLIB and r != "src"}
        if third_party:
            offenders[path.name] = sorted(third_party)
    assert not offenders, f"kernel has third-party dependencies: {offenders}"
