"""纯净度守卫：kernel 只能依赖 Python 标准库和 src 自身。

这条测试把“机制在内、策略在外、内核不认识任何厂商 SDK”变成可执行的约束：
谁不小心在 kernel 里 import 了 openai/httpx/第三方库，CI 立刻变红。
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
        third_party = {r for r in imported_roots(path)
                       if r not in STDLIB and r != "src"}
        if third_party:
            offenders[path.name] = sorted(third_party)
    assert not offenders, f"kernel 出现第三方依赖：{offenders}"
