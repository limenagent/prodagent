"""call_id is unique per call within one node execution (a shared id silently
cross-talks tool results); it is not stable across retries — see ToolCall."""

from src.kernel import FnBody, Node, Outcome, Plan, RunState, Scheduler
from src.runtime.tools import ToolRegistry, ToolSpec


async def test_two_calls_in_one_execution_get_distinct_ids():
    registry = ToolRegistry()
    registry.add(
        ToolSpec(
            name="echo",
            description="echo the name back",
            func=lambda name, ctx=None: f"hi {name}",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        )
    )
    results = []

    async def body(_x, ctx):
        results.append(await ctx.call_tool("echo", {"name": "a"}))
        results.append(await ctx.call_tool("echo", {"name": "b"}))
        return Outcome.ok("done")

    sch = Scheduler(tools=registry)
    run = await sch.run(Plan().add(Node("n", FnBody(body), terminal=True)))
    assert run.state == RunState.COMPLETED
    assert [r.output for r in results] == ["hi a", "hi b"]
    assert results[0].call_id != results[1].call_id  # one id per call, not per attempt
