"""Trip Planner 子 Agent —— 3 个专家各管一摊。

  - itinerary: 排 7 天行程(住哪 + 每天去哪 + 天气适应)
  - restaurant: 订餐厅(按用户偏好 cuisine)
  - transport: 订交通(航班 + 城际火车)

三个都是 ReAct —— 由主 agent 在第一个 turn 并行 spawn_agent 委派。
"""

from __future__ import annotations

from prodagent import Agent, AgentConfig, HardBudget

from trip_planner.tools import (
    book_restaurant,
    get_weather,
    search_flights,
    search_hotels,
    search_restaurants,
    search_trains,
)


def itinerary_peer_agent() -> Agent:
    """行程 peer —— 排 7 天行程 + 选酒店 + 按天气换活动。"""
    return Agent(
        "itinerary",
        system_prompt=(
            "你是日本行程规划专家。拿到用户偏好(duration / budget / interests / "
            "cities)后:1) search_hotels 选酒店;2) get_weather 看每天天气;"
            "3) 输出按城市分配天数的行程,雨天替换室外活动。"
            "用紧凑 JSON 返回: {\"hotel\": \"...\", \"nights\": <n>, "
            "\"days\": [{\"day\": 1, \"city\": \"...\", \"weather\": \"...\", "
            "\"activities\": [...]}], \"total_hotel_cost\": <n>}。"
        ),
        tools=[search_hotels, get_weather],
        budget=HardBudget(max_seconds=300),
        config=AgentConfig(
            name="itinerary",
            description="只读行程规划专家。选酒店 + 排行程 + 天气适应。",
        ),
    )


def restaurant_peer_agent() -> Agent:
    """餐厅 peer —— 按用户偏好订餐厅。"""
    return Agent(
        "restaurant",
        system_prompt=(
            "你是日本餐厅预订专家。拿到用户偏好(cuisine 偏好 + 每个城市的天数)后:"
            "1) search_restaurants 找餐厅;2) 对每天的主餐厅 book_restaurant。"
            "用紧凑 JSON 返回: {\"bookings\": [{\"day\": 1, \"city\": \"...\", "
            "\"restaurant\": \"...\", \"cuisine\": \"...\", \"price\": <n>}], "
            "\"total_cost\": <n>}。"
        ),
        tools=[search_restaurants, book_restaurant],
        budget=HardBudget(max_seconds=300),
        config=AgentConfig(
            name="restaurant",
            description="餐厅预订专家。按偏好找 + 订主餐厅。",
        ),
    )


def transport_peer_agent() -> Agent:
    """交通 peer —— 排航班 + 城际火车。"""
    return Agent(
        "transport",
        system_prompt=(
            "你是日本交通规划专家。拿到用户偏好(origin / cities 顺序 / 日期)后:"
            "1) search_flights 排往返航班;2) search_trains 排城际火车。\n"
            "工具接受城市名(Tokyo/Osaka/Kyoto)或 IATA 代码,日期接受多种格式。\n"
            "重要:数据库里只有有限路线。如果 search 返回 count=0,看 hint 字段里"
            "列出的可用路线,选一个最接近用户意图的,**不要无限重试不同格式**。\n"
            "拿到结果(哪怕只有一条)就组装 JSON 返回,不要追求完美。\n"
            "用紧凑 JSON 返回: {\"flights\": [{\"from\": \"...\", \"to\": \"...\", "
            "\"date\": \"...\", \"price\": <n>}], \"trains\": [{\"route\": \"...\", "
            "\"price\": <n>}], \"total_cost\": <n>}。"
        ),
        tools=[search_flights, search_trains],
        budget=HardBudget(max_seconds=300),
        config=AgentConfig(
            name="transport",
            description="只读交通规划专家。航班 + 城际火车。",
        ),
    )


def all_peer_agents() -> list[Agent]:
    """3 个 peer —— 传给 ``AgentConfig(agents=[...])`` + workflow 里 ``wf.step(peer)``。"""
    return [itinerary_peer_agent(), restaurant_peer_agent(), transport_peer_agent()]
