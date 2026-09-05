"""05 代码侦探 —— MCP 工具在边界拉平、技能从磁盘目录加载、失败再改直到通过。

- 代码仓库能力（读文件、grep、打补丁、跑测试）由一个进程内 MCP Server 提供，
  在边界被拉平成普通工具，走同一条调度管线；
- “怎么排障”的技能不写死在代码里，而是从 builtin_skills 目录的 SKILL.md 加载，
  技能因此可以独立增删、渐进披露，而框架一行都不用改。

跑法：PYTHONPATH=. python3 examples/05_code_detective.py
"""

import asyncio
import os

import src.runtime as runtime_pkg
from src import Agent
from src.kernel import ToolCall
from src.runtime.llm import ScriptedLlm, env_llm
from src.runtime.mcp import InProcessMCPServer, load_mcp_tools
from src.runtime.skills import SkillRegistry
from src.runtime.tools import ToolRegistry


async def main():
    # 1) 进程内 MCP Server 扮演“代码仓库”这套外部工具，在边界拉平进统一注册表。
    repo = InProcessMCPServer("repo")
    repo.define("read_file", lambda a: f"【{a['file']} 的内容】", description="读文件")
    repo.define("grep", lambda a: f"在 {a['pattern']} 处命中", description="全文检索")
    repo.define("apply_patch", lambda a: "补丁已应用", description="修改代码")
    runs = {"n": 0}

    def run_test(a):
        runs["n"] += 1
        return "测试通过" if runs["n"] >= 2 else "1 个测试仍失败：边界没处理"

    repo.define("run_test", run_test, description="运行测试")

    registry = ToolRegistry()
    await load_mcp_tools(registry, repo)

    # 2) 从磁盘目录加载技能：每个子目录一份 SKILL.md，就是一个可插拔的专长。
    skills_dir = os.path.join(os.path.dirname(runtime_pkg.__file__), "builtin_skills")
    skills = SkillRegistry()
    skills.load_dir(skills_dir)
    skill = skills.match("测试失败 排障 补丁 重跑")
    system = skills.apply_to_system(skill, "你是代码排障助手。")

    # 3) 模型剧本：第一轮补丁没修好，看到测试反馈后再改，第二次转绿。
    agent = Agent(
        name="detective",
        model=env_llm(
            ScriptedLlm(
                [
                    ToolCall("read_file", {"file": "test_x.py"}),
                    ToolCall("grep", {"pattern": "func_x"}),
                    ToolCall("read_file", {"file": "x.py"}),
                    ToolCall("apply_patch", {"change": "补边界"}),
                    ToolCall("run_test", {}),
                    ToolCall("apply_patch", {"change": "再补空值"}),
                    ToolCall("run_test", {}),
                    "定位到空值边界问题，两次修改后测试全部通过。",
                ]
            )
        ),
        instruction=system,
        registry=registry,
    )

    result = await agent.run("test_x 一直红，帮我修好")
    print("结论：", result.output)
    print(
        f"加载技能：{skill.name}｜工具调用 {result.metrics['tool_calls']} 次（跑测试 {runs['n']} 次）"
    )


if __name__ == "__main__":
    asyncio.run(main())
