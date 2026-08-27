"""Council —— agent 自己召集协作的端到端示例。

本示例展示:
  - 协作不只是应用层拉 stream：把带名字的 ``EnsembleSpec`` /
    ``WorkQueueSpec`` 声明在 ``AgentConfig`` 上，框架自动生成
    ``run_ensemble`` / ``run_work_queue`` 工具，模型像调 ``spawn_agent``
    一样自己决定开会、自己派活。
  - 成员/工人是普通 ``Agent``（``AgentFloorMember`` / ``AgentWorkMember``
    适配），发言与领活都经 RunnerPort 激活。
  - 默认 FakeLLM 离线可跑；换真模型只需 ``resolve_llm``。

运行（离线）:
    uv run python -m council "要不要周五上线"
"""

from __future__ import annotations

import asyncio
import sys

from prodagent import Agent, AgentConfig, ExecutionMode
from prodagent.coordination.ensemble import AgentFloorMember, EnsembleSpec
from prodagent.coordination.infra.stage import MaxRounds, TerminationPolicy
from prodagent.coordination.work_queue import AgentWorkMember, WorkQueueSpec
from prodagent.kernel.types import LLMResponse, StopReason, ToolCall
from prodagent.llm.fake import FakeLLMAdapter


def _member(name: str, stance: str) -> Agent:
    return Agent(
        name,
        system_prompt=f"你是评审组成员，立场：{stance}。一句话表态。",
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(
            name=name,
            llm=FakeLLMAdapter(),
        ),
    )


def _worker(name: str) -> Agent:
    return Agent(
        name,
        system_prompt="你是执行工人，收到任务直接完成并简报。",
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(name=name, llm=FakeLLMAdapter()),
    )


def build_council_agent() -> Agent:
    panel = EnsembleSpec(
        name="panel",
        members=[
            AgentFloorMember(_member("pro", "支持上线"), session_id="council-pro"),
            AgentFloorMember(_member("con", "反对上线"), session_id="council-con"),
        ],
        topic="待定议题",
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=2)),
    )
    chores = WorkQueueSpec(
        name="chores",
        workers={"w1": AgentWorkMember(_worker("w1")), "w2": AgentWorkMember(_worker("w2"))},
        items=[],
    )
    # 离线脚本：先召集评审，再派两件杂务，最后总结。接真模型时删掉 responses。
    scripted = FakeLLMAdapter(
        responses=[
            LLMResponse(
                content="",
                tool_calls=[ToolCall(name="run_ensemble", params={"name": "panel", "task": "议题"})],
                stop_reason=StopReason.TOOL_USE,
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(name="run_work_queue", params={"name": "chores", "items": ["写公告", "回滚预案"]})
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            LLMResponse(content="评审完成，杂务已分派。", stop_reason="end_turn"),
        ]
    )
    return Agent(
        "council",
        system_prompt=(
            "你是主持人。先召集 panel 评审议题，再视结论用 run_work_queue "
            "把后续工作分派给 chores。"
        ),
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(
            name="council",
            llm=scripted,
            ensembles=[panel],
            work_queues=[chores],
        ),
    )


async def main(topic: str) -> None:
    agent = build_council_agent()
    run = await agent.chat(topic, session_id="council-demo")
    print(f"[council] final: {run.final_output}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "要不要周五上线"))
