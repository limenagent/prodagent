"""Code detective — MCP tools normalized at the boundary, skills loaded
from a directory on disk, fix-fail-rerun until green.

- The repo capabilities (read files, grep, patch, run tests) are provided by
  an in-process MCP server, normalized at the boundary into ordinary tools
  that go through one scheduling pipeline;
- the "how to debug" skill is not hard-coded — it loads from a SKILL.md in
  the builtin_skills directory, so skills can be added and removed
  independently and disclosed progressively, with zero framework change.

Run: PYTHONPATH=. python3 examples/code_detective.py
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
    # 1) An in-process MCP server plays the external "code repo" toolset,
    #    normalized at the boundary into the unified registry.
    repo = InProcessMCPServer("repo")
    repo.define("read_file", lambda a: f"[contents of {a['file']}]", description="read a file")
    repo.define("grep", lambda a: f"hits at {a['pattern']}", description="full-text search")
    repo.define("apply_patch", lambda a: "patch applied", description="modify code")
    runs = {"n": 0}

    def run_test(a):
        runs["n"] += 1
        return "tests pass" if runs["n"] >= 2 else "1 test still failing: boundary not handled"

    repo.define("run_test", run_test, description="run the tests")

    registry = ToolRegistry()
    await load_mcp_tools(registry, repo)

    # 2) Load skills from a directory on disk: each subdirectory with a
    #    SKILL.md is one pluggable expertise. (The query stays Chinese — word
    #    matching against the bundled Chinese SKILL.md.)
    skills_dir = os.path.join(os.path.dirname(runtime_pkg.__file__), "builtin_skills")
    skills = SkillRegistry()
    skills.load_dir(skills_dir)
    skill = skills.match("测试失败 排障 补丁 重跑")
    system = skills.apply_to_system(skill, "You are a code-debugging assistant.")

    # 3) Model script: the first patch doesn't fix it; after seeing the test
    #    feedback it patches again; the second run goes green.
    agent = Agent(
        name="detective",
        model=env_llm(
            ScriptedLlm(
                [
                    ToolCall("read_file", {"file": "test_x.py"}),
                    ToolCall("grep", {"pattern": "func_x"}),
                    ToolCall("read_file", {"file": "x.py"}),
                    ToolCall("apply_patch", {"change": "guard the boundary"}),
                    ToolCall("run_test", {}),
                    ToolCall("apply_patch", {"change": "also guard the None case"}),
                    ToolCall("run_test", {}),
                    "Found a None-boundary bug; after two fixes all tests pass.",
                ]
            )
        ),
        instruction=system,
        registry=registry,
    )

    result = await agent.run("test_x keeps failing; fix it for me")
    print("Conclusion:", result.output)
    print(
        f"skill loaded: {skill.name} | tool calls: {result.metrics['tool_calls']} "
        f"(test runs: {runs['n']})"
    )


if __name__ == "__main__":
    asyncio.run(main())
