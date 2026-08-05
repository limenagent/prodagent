"""Code Detective MCP server —— 暴露文件系统 + 测试运行工具。

独立运行(测试 server 本身)::

    uv run python -m code_detective.mcp_server

当 code_detective Agent 用 ``mcp=[...]`` 启动时,框架把这个脚本作为
子进程 spawn,通过 stdin/stdout 说 MCP,把 server 的工具桥接进
Agent 的工具注册表,名字是 ``mcp__code_detective__<tool>``。

工具:
  - ``read_file(path)``        读 fixture repo 里的文件
  - ``grep(pattern, path)``    正则搜索,返回 [{file, line, content}]
  - ``run_tests(test_path)``   跑一个测试文件,返回 {passed, failures, output}
  - ``apply_patch(path, content)`` 覆写文件(写操作)

安全: 所有路径都被限制在 fixture repo 目录内,防 SSRF/路径穿越。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

_FIXTURE_ROOT = Path(__file__).parent / "fixture"


def _resolve(path: str) -> Path:
    """把相对路径解析到 fixture repo 内,防路径穿越。"""
    p = (_FIXTURE_ROOT / path).resolve()
    try:
        p.relative_to(_FIXTURE_ROOT.resolve())
    except ValueError as exc:
        raise PermissionError(f"path {path!r} escapes fixture repo") from exc
    return p


# ── 工具实现 ─────────────────────────────────────────────────────────────────


def read_file(path: str) -> str:
    """读 fixture repo 里的文件全文。"""
    try:
        p = _resolve(path)
    except PermissionError as exc:
        return f"Error: {exc}"
    if not p.exists():
        return f"Error: file not found: {path}"
    return p.read_text(encoding="utf-8")


def grep(pattern: str, path: str = ".") -> str:
    """正则搜索 fixture repo,返回匹配行。

    Args:
        pattern: 正则表达式。
        path: 搜索起点(相对 fixture root),默认整个 repo。
    """
    try:
        p = _resolve(path)
        regex = re.compile(pattern)
    except (PermissionError, re.error) as exc:
        return f"Error: {exc}"
    hits: list[dict[str, Any]] = []
    if p.is_file():
        files = [p]
    else:
        files = [f for f in p.rglob("*.py") if "__pycache__" not in f.parts]
    for f in files:
        try:
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if regex.search(line):
                    rel = f.relative_to(_FIXTURE_ROOT)
                    hits.append({"file": str(rel), "line": i, "content": line.strip()})
        except (OSError, UnicodeDecodeError):
            continue
    return json.dumps({"matches": hits, "count": len(hits)}, ensure_ascii=False)


def run_tests(test_path: str) -> str:
    """跑一个测试文件,返回结果。

    用子进程跑 ``python <test_path>``,捕获 stdout/stderr + exit code。
    """
    try:
        p = _resolve(test_path)
    except PermissionError as exc:
        return json.dumps({"passed": False, "failures": [str(exc)], "output": ""})
    if not p.exists():
        return json.dumps({"passed": False, "failures": [f"not found: {test_path}"], "output": ""})
    try:
        proc = subprocess.run(
            [sys.executable, str(p)],
            capture_output=True, text=True, timeout=15, cwd=str(_FIXTURE_ROOT),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return json.dumps({"passed": False, "failures": ["timeout after 15s"], "output": ""})
    passed = proc.returncode == 0
    output = (proc.stdout + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else "")).strip()
    failures = [] if passed else [line for line in output.splitlines() if "Error" in line or "assert" in line.lower()][:5]
    return json.dumps({
        "passed": passed, "failures": failures, "output": output[:2000],
        "exit_code": proc.returncode,
    }, ensure_ascii=False)


def apply_patch(path: str, content: str) -> str:
    """覆写 fixture repo 里的文件(写操作)。

    Args:
        path: 要写的文件(相对 fixture root)。
        content: 完整的新文件内容(不是 diff,是全文覆写)。
    """
    try:
        p = _resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except (PermissionError, OSError) as exc:
        return json.dumps({"applied": False, "error": str(exc)})
    return json.dumps({"applied": True, "path": path, "chars": len(content)})


# ── MCP server (JSON-RPC over stdio) ─────────────────────────────────────────

_TOOLS = [
    {
        "name": "read_file",
        "description": "读 fixture repo 里的文件全文。path 相对 fixture root(如 'auth.py', 'tests/test_user.py')。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对 fixture root 的文件路径"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "grep",
        "description": "正则搜索 fixture repo,返回匹配的行。用于定位函数/变量定义。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "path": {"type": "string", "description": "搜索起点(相对 fixture root),默认 '.'"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "run_tests",
        "description": "跑一个测试文件,返回 {passed, failures, output}。用 exit code 判定通过。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "test_path": {"type": "string", "description": "测试文件路径(如 'tests/test_user.py')"},
            },
            "required": ["test_path"],
        },
    },
    {
        "name": "apply_patch",
        "description": "覆写 fixture repo 里的文件(全文覆写,不是 diff)。用于应用修复 patch。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要写的文件路径"},
                "content": {"type": "string", "description": "完整的新文件内容"},
            },
            "required": ["path", "content"],
        },
    },
]


def _read_request() -> dict[str, Any] | None:
    line = sys.stdin.readline()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _write_response(msg: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method", "")
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2025-06-18",
                "serverInfo": {"name": "code-detective-mcp", "version": "1.0.0"},
                "capabilities": {"tools": {}},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": _TOOLS}}
    if method == "tools/call":
        params = request.get("params", {})
        name = params.get("name", "")
        args = params.get("arguments", {})
        if name == "read_file":
            result = read_file(args.get("path", ""))
        elif name == "grep":
            result = grep(args.get("pattern", ""), args.get("path", "."))
        elif name == "run_tests":
            result = run_tests(args.get("test_path", ""))
        elif name == "apply_patch":
            result = apply_patch(args.get("path", ""), args.get("content", ""))
        else:
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32601, "message": f"unknown tool {name!r}"}}
        return {"jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": result}]}}
    return None


def _main() -> None:
    """读循环 —— 每行一个 JSON-RPC 请求。"""
    while True:
        request = _read_request()
        if request is None:
            break
        response = _handle(request)
        if response is not None:
            _write_response(response)


if __name__ == "__main__":
    _main()
