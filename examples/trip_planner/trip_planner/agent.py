"""Trip Planner —— Workflow DAG + peer handoff + 长期记忆。

DAG::

    s1 parse_prefs (llm_step)
      ├── s2 itinerary peer  (spawn_agent)
      ├── s3 restaurant peer (spawn_agent)
      └── s4 transport peer  (spawn_agent)
    s5 merge_budget (llm_step)
    s6 weather_adjust (llm_step)
    s7 final_itinerary (llm_step, terminal)

本示例展示:
  - ``Workflow`` + ``wf.llm_step``: DAG 写死,s1/s5/s6/s7 是真 LLM 节点。
  - ``wf.step(peer_agent)``: s2/s3/s4 是子 agent,编译成 ``spawn_agent`` step,
    并行 fan-out(depends_on s1)。
  - ``MemoryManager + MemoryHooks``: 预置 PREFERENCE(用户偏好拉面/漫画),
    recall 注入让 restaurant peer 知道 cuisine 偏好。
  - ``RoutingFakeLLM``: 父 + 3 peer 共享 LLM,按 system prompt 分发到
    per-agent 队列,避免并发 spawn race。
"""

from __future__ import annotations

from pathlib import Path

from prodagent import (
    Agent,
    AgentConfig,
    FrameworkConfig,
    HardBudget,
    MemoryManager,
    RoutingFakeLLM,
    use_fake_llm,
)
from prodagent.backends.factory import resolve_aux_llm
from prodagent.skills.registry import SkillRegistry
from prodagent.hooks.bundles.memory import MemoryHooks

from trip_planner.fake_llm import build_fake_llm
from trip_planner.memory import build_memory
from trip_planner.peer_agents import (
    itinerary_peer_agent,
    restaurant_peer_agent,
    transport_peer_agent,
)

_BASE = Path(__file__).parent
SKILLS_DIR = _BASE / "skills"

_SYSTEM_PROMPT = """\
你是旅行规划编排 agent。用户给一段自由文本需求,你产出完整行程。

流程(ReAct):
1. 第一轮: 从需求里解析结构化偏好(duration/budget/cities/interests/origin),
   写进 task,并在同一个 turn 内发出全部三个 spawn_agent 调用
   (不要等一个再调下一个):
   spawn_agent(name='itinerary', task=<偏好+排行程+选酒店要求>)
   spawn_agent(name='restaurant', task=<偏好+按城市订餐要求>)
   spawn_agent(name='transport', task=<往返航班+城际火车要求>)
2. 三个 spawn_agent 的工具结果就是三份 JSON 报告,直接读。
3. 合并与收尾由你自己完成(不再委派):
   - 预算检查: 住宿+餐厅+交通 vs 用户预算,超支要给调整建议;
   - 天气: 雨天活动替换成室内;
   - 最终报告: 每天 城市/酒店/餐厅/活动/交通 + 总花费 vs 预算。

预置记忆里有用户偏好(拉面/漫画/酒店靠近车站),recall 会注入到子 agent 的
task 里,让 restaurant 子 agent 知道订拉面店。
"""


DEFAULT_TASK = "7 天日本旅行,预算 15000,喜欢拉面和漫画。"


def build_trip_planner_agent(
    *,
    memory: MemoryManager | None = None,
    framework_config: FrameworkConfig | None = None,
) -> Agent:
    """组装 Trip Planner Agent。

    Args:
        memory: 预 seeded 的 MemoryManager(demo 用)。playground 不传。
        framework_config: 父 fw;不传时用 default。playground 注入带独立 namespace 的 fw。
    """
    from prodagent.base.config import production

    fw = framework_config or production()
    skills = SkillRegistry.from_dir(SKILLS_DIR)
    resolved_memory = memory or build_memory(
        aux_llm=resolve_aux_llm(fw), framework_config=fw, clean=True
    )

    itinerary = itinerary_peer_agent()
    restaurant = restaurant_peer_agent()
    transport = transport_peer_agent()

    llm: RoutingFakeLLM | None = build_fake_llm() if use_fake_llm() else None

    return Agent(
        "trip_planner",
        system_prompt=_SYSTEM_PROMPT,
        tools=[],
        budget=HardBudget(max_turns=25, max_cost_usd=1.5, max_seconds=900.0),
        config=AgentConfig(
            name="trip_planner",
            skills=skills,
            llm=llm,
            framework=fw,
            agents=[itinerary, restaurant, transport],
            extensions=[MemoryHooks(resolved_memory)],
        ),
    )
