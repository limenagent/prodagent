"""Code detective — MCP tools normalized at the boundary, skills from disk.

- The repo capabilities (read, grep, patch, run tests) are provided by an
  in-process MCP server, normalized at the boundary into ordinary tools that go
  through the one scheduling pipeline;
- the "how to debug" skill is not hard-coded — it loads from a SKILL.md in this
  example's skills directory, so skills can be added/removed independently and
  disclosed progressively, with zero framework change;
- the model's first patch doesn't fix it; after the test feedback it patches
  again and the second run goes green (fix-fail-rerun).

``build(lang)`` is the single assembly point (bilingual, async); ``main`` runs
English. Run: PYTHONPATH=. python3 examples/code_detective.py
"""

import asyncio
import os

from src import Agent
from src.kernel import ToolCall
from src.runtime.llm import ScriptedLlm, env_llm
from src.runtime.mcp import InProcessMCPServer, load_mcp_tools
from src.runtime.skills import SkillRegistry
from src.runtime.tools import ToolRegistry


async def build(lang: str = "en") -> Agent:
    t = {
        "en": {
            "read_fmt": "[contents of {file}]",
            "grep_fmt": "hits at {pattern}",
            "patch_out": "patch applied",
            "read_desc": "read a file",
            "grep_desc": "full-text search",
            "patch_desc": "modify code",
            "test_desc": "run the tests",
            "tests_pass": "tests pass",
            "tests_fail": "1 test still failing: boundary not handled",
            "base_system": "You are a code-debugging assistant.",
            "script": [
                ToolCall("read_file", {"file": "test_x.py"}),
                ToolCall("grep", {"pattern": "func_x"}),
                ToolCall("read_file", {"file": "x.py"}),
                ToolCall("apply_patch", {"change": "guard the boundary"}),
                ToolCall("run_test", {}),
                ToolCall("apply_patch", {"change": "also guard the None case"}),
                ToolCall("run_test", {}),
                "Found a None-boundary bug; after two fixes all tests pass.",
            ],
        },
        "zh": {
            "read_fmt": "【{file} 的内容】",
            "grep_fmt": "在 {pattern} 处命中",
            "patch_out": "补丁已应用",
            "read_desc": "读文件",
            "grep_desc": "全文检索",
            "patch_desc": "修改代码",
            "test_desc": "运行测试",
            "tests_pass": "测试通过",
            "tests_fail": "1 个测试仍失败：边界没处理",
            "base_system": "你是代码排障助手。",
            "script": [
                ToolCall("read_file", {"file": "test_x.py"}),
                ToolCall("grep", {"pattern": "func_x"}),
                ToolCall("read_file", {"file": "x.py"}),
                ToolCall("apply_patch", {"change": "补边界"}),
                ToolCall("run_test", {}),
                ToolCall("apply_patch", {"change": "再补空值"}),
                ToolCall("run_test", {}),
                "定位到空值边界问题，两次修改后测试全部通过。",
            ],
        },
    }[lang]

    repo = InProcessMCPServer("repo")
    repo.define(
        "read_file", lambda a: t["read_fmt"].format(file=a["file"]), description=t["read_desc"]
    )
    repo.define(
        "grep", lambda a: t["grep_fmt"].format(pattern=a["pattern"]), description=t["grep_desc"]
    )
    repo.define("apply_patch", lambda a: t["patch_out"], description=t["patch_desc"])
    runs = {"n": 0}

    def run_test(a):
        runs["n"] += 1
        return t["tests_pass"] if runs["n"] >= 2 else t["tests_fail"]

    repo.define("run_test", run_test, description=t["test_desc"])

    registry = ToolRegistry()
    await load_mcp_tools(registry, repo)

    # The bundled skill is Chinese, so the match query stays Chinese in both
    # UI languages (word-overlap matching would miss it in English).
    skills_dir = os.path.join(os.path.dirname(__file__), "skills")
    skills = SkillRegistry()
    skills.load_dir(skills_dir)
    skill = skills.match("测试失败 排障 补丁 重跑")
    system = skills.apply_to_system(skill, t["base_system"])

    return Agent(
        name="detective",
        model=env_llm(ScriptedLlm(list(t["script"]))),
        instruction=system,
        registry=registry,
    )


async def main():
    agent = await build("en")
    result = await agent.run("test_x keeps failing; fix it for me")
    print("Conclusion:", result.output)
    print(f"tool calls: {result.metrics['tool_calls']}")


if __name__ == "__main__":
    asyncio.run(main())
