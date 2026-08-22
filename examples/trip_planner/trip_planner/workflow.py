"""Trip Planner workflow —— DAG 写死,s2/s3/s4 并行 spawn 3 个 peer。

DAG::

    s1 parse_prefs (llm_step —— LLM 抽 duration/budget/interests/cities)
      ├── s2 itinerary peer  (spawn_agent —— 排行程 + 选酒店)
      ├── s3 restaurant peer (spawn_agent —— 订餐厅)
      └── s4 transport peer  (spawn_agent —— 订交通)
    s5 merge_budget (llm_step —— 合 3 份结果,检查预算)
    s6 weather_adjust (llm_step —— 按雨天调整活动)
    s7 final_itinerary (llm_step —— 输出最终行程,terminal)

s2/s3/s4 并行(都 depends_on s1),s5 merge,s6/s7 串行。
workflow 编译成 Plan,跳过 LLM planning —— DAG 是 plan,peer 即步骤。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prodagent.plan.workflow import Workflow

if TYPE_CHECKING:
    from prodagent.runtime.agent import Agent

_PARSE_SYSTEM = (
    "你是旅行偏好解析器。把用户自由文本解析成结构化 prefs。"
    "用紧凑 JSON 返回: {\"duration_days\": <n>, \"budget\": <n>, "
    "\"cities\": [\"tokyo\", \"osaka\", ...], \"interests\": [\"ramen\", ...], "
    "\"origin\": \"PVG\"}。不要包装在 markdown 里。"
)

_PARSE_PROMPT = (
    "解析这个旅行请求:\n\n"
    "{{task}}\n\n"
    "默认 origin=PVG,7 天行程预算 15000,城市顺序合理。"
)

_MERGE_SYSTEM = (
    "你是预算审计员。给定 3 个 peer 的输出 + 用户预算,检查是否超支。"
    "用紧凑 JSON 返回: {\"over_budget\": <bool>, \"total_cost\": <n>, "
    "\"budget\": <n>, \"suggestion\": \"...\"}。"
)

_MERGE_PROMPT = (
    "合并 3 个 peer 的结果,检查预算:\n\n"
    "=== itinerary ===\n{{s2.output}}\n\n"
    "=== restaurant ===\n{{s3.output}}\n\n"
    "=== transport ===\n{{s4.output}}\n\n"
    "用户预算来自 s1: {{s1.output}}"
)

_WEATHER_SYSTEM = (
    "你是行程优化器。如果 itinerary 里有雨天活动,替换成室内活动。"
    "用紧凑 JSON 返回: {\"adjusted\": <bool>, \"changes\": [...], "
    "\"final_itinerary\": \"<markdown>\"}。"
)

_WEATHER_PROMPT = (
    "按天气调整行程:\n\n"
    "=== 合并结果 ===\n{{s5.output}}\n\n"
    "看 itinerary 里的雨天,把室外活动换成室内(博物馆 / 商场 / 温泉)。"
)

_FINAL_SYSTEM = "你是旅行作家。把调整后的行程输出成最终 markdown 行程表。"

_FINAL_PROMPT = (
    "输出最终行程表:\n\n"
    "=== 调整后 ===\n{{s6.output}}\n\n"
    "格式:\n"
    "# 日本 7 天行程\n\n"
    "## 总览\n- 总预算: ...\n- 总花费: ...\n\n"
    "## Day 1 - Day 7\n每天: 城市 / 酒店 / 主餐厅 / 活动 / 交通\n"
)


def build_trip_workflow(
    itinerary: Agent,
    restaurant: Agent,
    transport: Agent,
) -> Workflow:
    """构建 trip planner workflow。

    Args:
        itinerary / restaurant / transport: 3 个 peer agent,workflow 里作为
            ``wf.step(peer)`` 编译成 ``spawn_agent`` step。
    """
    wf = Workflow()

    # s1: LLM 解析偏好(terminal=False,output 喂给 s2/s3/s4)。
    wf.llm_step("s1", _PARSE_PROMPT, system=_PARSE_SYSTEM)

    # s2/s3/s4: 3 个 peer 并行(depends_on s1)。task 含「拉面」「漫画」
    # 关键词,触发 Memory recall 注入用户偏好。
    wf.step(
        itinerary,
        name="s2",
        depends_on=["s1"],
        params={"task": "排 7 天日本行程(住 + 每天 + 天气),用户喜欢拉面和漫画: {{s1.output}}"},
    )
    wf.step(
        restaurant,
        name="s3",
        depends_on=["s1"],
        params={"task": "按偏好(拉面 / 漫画)订主餐厅: {{s1.output}}"},
    )
    wf.step(
        transport,
        name="s4",
        depends_on=["s1"],
        params={"task": "排航班 + 城际火车(用户偏好新干线): {{s1.output}}"},
    )

    # s5: 合并 + 预算检查(depends_on s2/s3/s4)。
    wf.llm_step(
        "s5", _MERGE_PROMPT, system=_MERGE_SYSTEM,
        depends_on=["s2", "s3", "s4"],
    )

    # s6: 天气调整(depends_on s5)。
    wf.llm_step("s6", _WEATHER_PROMPT, system=_WEATHER_SYSTEM, depends_on=["s5"])

    # s7: 最终行程表(terminal)。
    wf.llm_step(
        "s7", _FINAL_PROMPT, system=_FINAL_SYSTEM,
        depends_on=["s6"], is_terminal=True,
    )

    return wf


__all__ = ["build_trip_workflow"]
