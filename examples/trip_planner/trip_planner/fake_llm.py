"""Trip Planner FakeLLM 脚本 —— 路由机制用框架的 ``RoutingFakeLLM``。

父 agent + 3 个 peer 共享 LLM,但每个需要不同的响应。RoutingFakeLLM 嗅探
system prompt 里的 ``# {name} Agent``,分发到该 agent 的 per-call 队列。

每个 peer 2 turn(工具调用 → JSON 总结),父 agent 在 workflow 模式下不调 LLM
生成 plan(workflow 编译成 Plan),所以 LLM 调用全部来自 ``wf.llm_step`` 和
peer agent 的 ReAct 循环。

各步 LLM 调用来源:
  - s1/s5/s6/s7: workflow ``wf.llm_step`` —— system 是 ``_PARSE_SYSTEM`` /
    ``_MERGE_SYSTEM`` / ``_WEATHER_SYSTEM`` / ``_FINAL_SYSTEM``,不在 routing
    范围内,走 default 队列(按调用顺序取)。
  - s2/s3/s4: peer agent 的 ReAct —— system 是 ``# itinerary Agent`` /
    ``# restaurant Agent`` / ``# transport Agent``,走各自队列。
"""

from __future__ import annotations

from prodagent import RoutingFakeLLM
from prodagent.kernel.types import LLMResponse, ToolCall


# ── 各 peer 的 JSON 总结 ────────────────────────────────────────────────────

_ITINERARY_JSON = (
    '{"hotel":"Shinjuku Gran","nights":6,"days":['
    '{"day":1,"city":"tokyo","weather":"sunny","activities":["浅草寺","秋叶原漫画街"]},'
    '{"day":2,"city":"tokyo","weather":"rain","activities":["teamLab Borderless","涩谷购物中心"]},'
    '{"day":3,"city":"tokyo","weather":"sunny","activities":["明治神宫","新宿拉面街"]},'
    '{"day":4,"city":"osaka","weather":"cloudy","activities":["大阪城","道顿堀"]},'
    '{"day":5,"city":"osaka","weather":"sunny","activities":["环球影城","黑门市场"]},'
    '{"day":6,"city":"kyoto","weather":"sunny","activities":["伏见稻荷","清水寺"]},'
    '{"day":7,"city":"kyoto","weather":"sunny","activities":["金阁寺","京都漫画博物馆"]}'
    '],"total_hotel_cost":108000}'
)

_RESTAURANT_JSON = (
    '{"bookings":['
    '{"day":1,"city":"tokyo","restaurant":"Ichiran Ramen","cuisine":"ramen","price":1200},'
    '{"day":2,"city":"tokyo","restaurant":"Sushi Saito","cuisine":"sushi","price":8000},'
    '{"day":3,"city":"tokyo","restaurant":"Ichiran Ramen","cuisine":"ramen","price":1200},'
    '{"day":4,"city":"osaka","restaurant":"Kushikatsu Daruma","cuisine":"kushikatsu","price":1800},'
    '{"day":5,"city":"osaka","restaurant":"Kushikatsu Daruma","cuisine":"kushikatsu","price":1800},'
    '{"day":6,"city":"kyoto","restaurant":"Kaiseki Gion","cuisine":"kaiseki","price":6500},'
    '{"day":7,"city":"kyoto","restaurant":"Kaiseki Gion","cuisine":"kaiseki","price":6500}'
    '],"total_cost":27000}'
)

_TRANSPORT_JSON = (
    '{"flights":['
    '{"from":"PVG","to":"NRT","date":"2026-08-01","price":3200},'
    '{"from":"NRT","to":"PVG","date":"2026-08-07","price":2900}'
    '],"trains":['
    '{"route":"tokyo → osaka","price":1400},'
    '{"route":"osaka → kyoto","price":600},'
    '{"route":"kyoto → tokyo","price":1400}'
    '],"total_cost":9500}'
)


# ── 父 agent(workflow llm_step)的 4 个响应:s1 parse / s5 merge / s6 weather / s7 final ──

_PARSE_JSON = (
    '{"duration_days":7,"budget":15000,"cities":["tokyo","osaka","kyoto"],'
    '"interests":["ramen","manga"],"origin":"PVG"}'
)

_MERGE_JSON = (
    '{"over_budget":false,"total_cost":144500,"budget":150000,'
    '"suggestion":"预算充裕 —— 酒店占 108000,餐厅 27000,交通 9500。可加一次温泉体验。"}'
)

_WEATHER_JSON = (
    '{"adjusted":true,"changes":['
    '{"day":2,"city":"tokyo","from":"明治神宫(室外)","to":"teamLab Borderless(室内)"},'
    '{"day":6,"city":"osaka","from":"环球影城(室外)","to":"海游馆(室内)"}'
    '],"final_itinerary":"调整后:Day2 室内,Day6 室内"}'
)

_FINAL_MARKDOWN = (
    "# 日本 7 天行程\n\n"
    "## 总览\n- 总预算: ¥150000\n- 总花费: ¥144500\n- 节省: ¥5500\n\n"
    "## Day 1-3:东京\n"
    "- 酒店: Shinjuku Gran(¥18000/晚)\n"
    "- Day 1: 浅草寺 → 秋叶原漫画街 → Ichiran Ramen\n"
    "- Day 2(雨): teamLab Borderless → 涩谷购物中心 → Sushi Saito\n"
    "- Day 3: 明治神宫 → 新宿拉面街 → Ichiran Ramen\n\n"
    "## Day 4-5:大阪\n"
    "- Day 4: 大阪城 → 道顿堀 → Kushikatsu Daruma\n"
    "- Day 5(雨): 海游馆 → 黑门市场 → Kushikatsu Daruma\n\n"
    "## Day 6-7:京都\n"
    "- Day 6: 伏见稻荷 → 清水寺 → Kaiseki Gion\n"
    "- Day 7: 金阁寺 → 京都漫画博物馆 → Kaiseki Gion\n\n"
    "## 交通\n- 航班: PVG→NRT ¥3200 / NRT→PVG ¥2900\n"
    "- 新干线: tokyo→osaka ¥1400 / osaka→kyoto ¥600 / kyoto→tokyo ¥1400\n"
)


def build_fake_llm() -> RoutingFakeLLM:
    """构建带 per-agent 脚本的 routing FakeLLM。

    父 agent(workflow llm_step)按调用顺序取 4 个响应:parse → merge → weather → final。
    3 个 peer 各 2 turn:工具调用 → JSON 总结。
    """
    llm = RoutingFakeLLM()

    # ── 父 agent —— workflow llm_step 的 4 次调用 ──
    llm.set_default([
        LLMResponse(content=_PARSE_JSON, stop_reason="end_turn"),    # s1 parse
        LLMResponse(content=_MERGE_JSON, stop_reason="end_turn"),    # s5 merge
        LLMResponse(content=_WEATHER_JSON, stop_reason="end_turn"),  # s6 weather
        LLMResponse(content=_FINAL_MARKDOWN, stop_reason="end_turn"),  # s7 final
    ])

    # ── itinerary peer —— 2 turn ──
    llm.add("itinerary", [
        LLMResponse(
            content="先搜酒店 + 看天气。",
            tool_calls=[
                ToolCall(name="search_hotels", params={"city": "tokyo", "max_price_per_night": 30000}),
                ToolCall(name="get_weather", params={
                    "city": "tokyo",
                    "dates": ["2026-08-01", "2026-08-02", "2026-08-03"],
                }),
            ],
            stop_reason="tool_use",
        ),
        LLMResponse(content=_ITINERARY_JSON, stop_reason="end_turn"),
    ])

    # ── restaurant peer —— 2 turn ──
    llm.add("restaurant", [
        LLMResponse(
            content="先搜东京拉面。",
            tool_calls=[
                ToolCall(name="search_restaurants", params={"city": "tokyo", "cuisine": "ramen"}),
            ],
            stop_reason="tool_use",
        ),
        LLMResponse(
            content="已订 7 天主餐厅。",
            tool_calls=[
                ToolCall(name="book_restaurant", params={
                    "restaurant": "Ichiran Ramen", "date": "2026-08-01", "party_size": 2,
                }),
            ],
            stop_reason="tool_use",
        ),
        LLMResponse(content=_RESTAURANT_JSON, stop_reason="end_turn"),
    ])

    # ── transport peer —— 2 turn ──
    llm.add("transport", [
        LLMResponse(
            content="先搜航班。",
            tool_calls=[
                ToolCall(name="search_flights", params={
                    "origin": "PVG", "dest": "NRT", "date": "2026-08-01",
                }),
                ToolCall(name="search_trains", params={"route": "tokyo → osaka"}),
            ],
            stop_reason="tool_use",
        ),
        LLMResponse(content=_TRANSPORT_JSON, stop_reason="end_turn"),
    ])

    return llm


__all__ = ["RoutingFakeLLM", "build_fake_llm"]
