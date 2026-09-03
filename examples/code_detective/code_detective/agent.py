"""Code Detective —— MCP 工具 + ReAct 多轮调试 + 学习合成的自主修 bug agent。

本示例展示:
  - ``mcp=[MCPServerConfig(...)]``: spawn ``code_detective.mcp_server`` 子进程,
    桥接 4 个工具(``read_file`` / ``grep`` / ``run_tests`` / ``apply_patch``)
    为 ``mcp__code_detective__<tool>``。
  - ``ReAct 多轮调试``: LLM 每 turn 发一个 tool_call,看结果后决定下一步。
    初始 patch 故意写错(漏 salt),run_tests 返回 passed=false,LLM 读错误信息后
    读 password.py 看正确实现,重提 patch,再跑测试通过。

fixture repo(``code_detective/fixture/``):
  - ``auth.py`` —— 含 bug:``login`` 用 ``hash(password)`` 不加 salt。
  - ``password.py`` —— 正确实现:``hash_with_salt(password, salt)``。
  - ``tests/test_user.py`` —— 验证 login,用 hash_with_salt 存期望值,所以 broken auth 必失败。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from prodagent import Agent, AgentConfig, FrameworkConfig, HardBudget, use_fake_llm
from prodagent.skills.registry import SkillRegistry
from prodagent.mcp.config import MCPServerConfig

from code_detective.fake_llm import build_fake_llm

_BASE = Path(__file__).parent
SKILLS_DIR = _BASE / "skills"
FIXTURE_ROOT = _BASE / "fixture"

_SYSTEM_PROMPT = """\
你是代码侦探 agent,专门自主修复失败的测试。

## 工作流
1. 调 ``get_skill(name="debug-workflow")`` 加载修 bug runbook(只调一次,加载后按 runbook 的 Turn 1 往下走,不要重复调 get_skill)。
2. 读失败的测试文件(``mcp__code_detective__read_file``)—— 看它断言什么、期望什么。
3. grep 定位相关函数(``mcp__code_detective__grep``)。
4. 读源文件理解现状。
5. 提 patch(``mcp__code_detective__apply_patch`` —— 全文覆写)。
6. 跑测试(``mcp__code_detective__run_tests``)。
7. 失败时读错误信息,可能要读更多文件(如正确的实现),换思路重提 patch。

## 规则
- ``get_skill`` 只调一次 —— 加载成功后立即按 runbook 的 Turn 1 调 ``mcp__code_detective__read_file``,不要重复加载。
- ``apply_patch`` 是全文覆写,不是 diff —— 传完整的新文件内容。
- 测试失败时,``run_tests`` 的 output 里有 traceback —— 读它定位真正的错误。
- 不要重复提同一个 patch —— 没修好就换思路。
- 每个 turn 发一个 tool_call,看结果后决定下一步 —— 不要在一个 turn 里堆多个 tool_call。
"""


def _code_detective_mcp_config() -> MCPServerConfig:
    """构建 MCP server 配置 —— spawn ``python -m code_detective.mcp_server``。

    env 传 PYTHONPATH 指向 examples/code_detective,让子进程能 import code_detective 包
    (playground 动态加载时父进程的 sys.path 不会自动继承到子进程)。
    """
    package_root = str(_BASE.parent)
    return MCPServerConfig(
        name="code_detective",
        transport="stdio",
        command=sys.executable,
        args=["-m", "code_detective.mcp_server"],
        env={"PYTHONPATH": package_root},
        timeout_ms=15_000,
    )


def _reset_fixture() -> None:
    """把 fixture/auth.py 重置成含 bug 的初始状态(让 demo 可重复跑)。"""
    src = FIXTURE_ROOT / "_auth_initial.py"
    if not src.exists():
        shutil.copy(FIXTURE_ROOT / "auth.py", src)
    else:
        shutil.copy(src, FIXTURE_ROOT / "auth.py")


DEFAULT_TASK = "tests/test_user.py::test_login 失败,帮我修。"


def _production_fw() -> FrameworkConfig:
    from prodagent.base.config import production

    return production(FrameworkConfig.default())


def build_code_detective_agent(
    *,
    framework_config: FrameworkConfig | None = None,
) -> Agent:
    """组装 Code Detective Agent。

    Console/Span/Memory/Approval/Learning 全部由
    ``Agent.attach_default_hooks`` 接线 —— example 不碰 hook bundle。
    LearningHooks 的 synthesizer + aux LLM 也由框架从 fw lazy resolve。
    """
    skills = SkillRegistry.from_dir(SKILLS_DIR)
    use_fake = use_fake_llm()
    llm = build_fake_llm() if use_fake else None
    _reset_fixture()

    return Agent(
        "code_detective",
        system_prompt=_SYSTEM_PROMPT,
        tools=[],
        budget=HardBudget(max_turns=20, max_cost_usd=0.80, max_seconds=300.0),
        config=AgentConfig(
            name="code_detective",
            skills=skills,
            llm=llm,
            framework=framework_config or _production_fw(),
            mcp=[_code_detective_mcp_config()],
        ),
    )
