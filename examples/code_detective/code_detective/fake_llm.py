"""Code Detective FakeLLM 脚本 —— REACTIVE 多轮修 bug 轨迹。

REACTIVE 模式下,每个 turn LLM 发 tool_call,框架执行后把结果喂回 LLM,
LLM 看结果决定下一步。这样能精确模拟「看测试失败 → 读更多源 → 重提 patch」的调试循环。

轨迹(7 turn):
  turn 1: get_skill("debug-workflow")        —— 加载 runbook
  turn 2: read_file("tests/test_user.py")    —— 读失败测试
  turn 3: grep("def login")                  —— 定位 login 函数
  turn 4: read_file("auth.py")               —— 读含 bug 的源
  turn 5: apply_patch(auth.py, BROKEN)       —— 故意提错 patch(漏 salt)
  turn 6: run_tests(...)                     —— 测试失败(passed=false)
  turn 7: read_file("password.py")           —— 看正确实现 → 换思路
  turn 8: apply_patch(auth.py, FIXED)        —— 提正确 patch
  turn 9: run_tests(...)                     —— 测试通过
  turn 10: 最终文本总结

aux_llm(用于 LearningHooks 合成 skill)返回 JSON,因为 SkillSynthesizer 期望结构化输出。
"""

from __future__ import annotations

import json

from prodagent.llm.base import LLMClient
from prodagent.llm.fake import script

# ── 初始 patch:故意写错(用 hash 而不是 hash_with_salt)────────────────────────
_BROKEN_AUTH = '''"""auth.py —— 含 bug 的认证模块(故意写错)。"""

from __future__ import annotations

_USERS: dict[str, str] = {
    "alice": "5f4dcc3b5aa765d61d8327deb882cf99",
    "bob": "098f6bcd4621d373cade4e832627b4f6",
}


def hash(password: str) -> str:
    """不安全的 hash —— 缺 salt。这是 bug 根源。"""
    import hashlib
    return hashlib.md5(password.encode()).hexdigest()


def login(username: str, password: str) -> bool:
    """修复尝试 1: 仍然用 hash(),不加 salt —— 还是匹配不上。"""
    hashed = hash(password)
    return _USERS.get(username) == hashed
'''

# ── 正确 patch:用 hash_with_salt ─────────────────────────────────────────────
_FIXED_AUTH = '''"""auth.py —— 修复后:用 hash_with_salt 替代不安全的 hash。"""

from __future__ import annotations

from password import hash_with_salt

_USERS: dict[str, str] = {
    "alice": "5f4dcc3b5aa765d61d8327deb882cf99",
    "bob": "098f6bcd4621d373cade4e832627b4f6",
}


def login(username: str, password: str) -> bool:
    """验证用户凭据 —— 用 hash_with_salt 校验。"""
    hashed = hash_with_salt(password)
    return _USERS.get(username) == hashed
'''


def build_fake_llm() -> LLMClient:
    """REACTIVE 修 bug 轨迹:加载 runbook → 读测试 → grep → 读源 → 错 patch → 测试失败
    → 读 password.py → 正确 patch → 测试通过 → 总结。"""
    return script(
        {"tool": "get_skill", "params": {"name": "debug-workflow"}},
        {"tool": "mcp__code_detective__read_file", "params": {"path": "tests/test_user.py"}},
        {"tool": "mcp__code_detective__grep", "params": {"pattern": "def login", "path": "."}},
        {"tool": "mcp__code_detective__read_file", "params": {"path": "auth.py"}},
        {"tool": "mcp__code_detective__apply_patch",
         "params": {"path": "auth.py", "content": _BROKEN_AUTH}},
        {"tool": "mcp__code_detective__run_tests",
         "params": {"test_path": "tests/test_user.py"}},
        {"tool": "mcp__code_detective__read_file", "params": {"path": "password.py"}},
        {"tool": "mcp__code_detective__apply_patch",
         "params": {"path": "auth.py", "content": _FIXED_AUTH}},
        {"tool": "mcp__code_detective__run_tests",
         "params": {"test_path": "tests/test_user.py"}},
        {"content": (
            "已修复 auth.py 的 login bug。\n\n"
            "## 根因\n"
            "``login`` 用 ``hash(password)`` 不加 salt,而测试用 ``hash_with_salt`` 存期望值,"
            "两者结果不匹配。\n\n"
            "## 修复\n"
            "从 ``password.py`` import ``hash_with_salt``,在 ``login`` 里用它替代 ``hash``。\n\n"
            "## 验证\n"
            "``tests/test_user.py`` 通过(alice 能登录,错密码/不存在用户被拒)。"
        ), "stop_reason": "end_turn"},
    )


def build_aux_fake_llm() -> LLMClient:
    """用于 LearningHooks 合成 skill 的辅助 LLM。

    SkillSynthesizer 期望 JSON,字段: name / description / content (markdown body)。
    """
    skill_json = json.dumps({
        "name": "test-login-failure",
        "description": "test_login 失败 → 检查 password hash 是否漏 salt",
        "content": (
            "## test_login Failure Runbook\n\n"
            "### 症状\n"
            "``tests/test_user.py::test_login`` 失败,assert login('alice', 'password') is True。\n\n"
            "### 诊断\n"
            "1. 读 auth.py 看 login 实现 —— 如果用 ``hash(password)`` 不加 salt,就是 bug。\n"
            "2. 读 password.py 看 ``hash_with_salt`` —— 这是正确实现。\n\n"
            "### 修复\n"
            "在 auth.py 里 ``from password import hash_with_salt``,login 里用它替代 hash。\n"
        ),
    }, ensure_ascii=False)
    return script({"content": skill_json, "stop_reason": "end_turn"})
