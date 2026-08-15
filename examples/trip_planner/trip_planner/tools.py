"""Trip Planner 工具 —— 3 个 peer 子 agent 各自调的只读搜索 + 订工具。

每个 peer agent 持有自己那部分工具:
  - itinerary_peer: search_hotels, get_weather
  - restaurant_peer: search_restaurants, book_restaurant
  - transport_peer: search_flights, search_trains

数据进程内 fake,示例离线可跑。
"""

from __future__ import annotations

import asyncio
import re

from prodagent import SideEffectLevel, ToolMeta, tool

# 第十章"隔离优于共享": 锁是 Tool 实现者自己的职责,框架执行器不管。
# restaurant-booking 资源用 Tool 内自持的 asyncio.Lock 串行化;忙时返回
# 结构化 resource_busy 反馈,由上层 LLM 决定让路还是稍后重试。
_BOOKING_LOCK = asyncio.Lock()
_BOOKING_LOCK_WAIT_S = 0.1  # 必须小于 estimated_latency_ms / 1000(外层还有工具超时)


async def _acquire_booking_lock() -> dict | None:
    """拿到 restaurant-booking 锁返回 None;拿不到返回 LLM 可读的 RESOURCE_BUSY 反馈。"""
    try:
        await asyncio.wait_for(_BOOKING_LOCK.acquire(), timeout=_BOOKING_LOCK_WAIT_S)
        return None
    except TimeoutError:
        return {
            "error": True,
            "reason": "resource_busy",
            "code": "resource_busy",
            "error_severity": "yellow",
            "message": "Resource 'restaurant-booking' is busy (held by another agent).",
            "hint": "Try an alternative task or retry later.",
        }

# ── 假数据库 ────────────────────────────────────────────────────────────────

_HOTELS = [
    {"name": "Shinjuku Gran", "area": "tokyo", "price_per_night": 18000, "rating": 4.6, "near_station": True},
    {"name": "Osaka Bay Hotel", "area": "osaka", "price_per_night": 12000, "rating": 4.3, "near_station": False},
    {"name": "Kyoto Ryokan Gin", "area": "kyoto", "price_per_night": 22000, "rating": 4.8, "near_station": True},
]

_RESTAURANTS = [
    {"name": "Ichiran Ramen", "city": "tokyo", "cuisine": "ramen", "price_per_person": 1200, "rating": 4.5},
    {"name": "Sushi Saito", "city": "tokyo", "cuisine": "sushi", "price_per_person": 8000, "rating": 4.9},
    {"name": "Kushikatsu Daruma", "city": "osaka", "cuisine": "kushikatsu", "price_per_person": 1800, "rating": 4.4},
    {"name": "Kaiseki Gion", "city": "kyoto", "cuisine": "kaiseki", "price_per_person": 6500, "rating": 4.7},
]

_FLIGHTS = [
    {"from": "PVG", "to": "NRT", "date": "2026-08-01", "price": 3200, "airline": "JAL", "duration_h": 3.2},
    {"from": "NRT", "to": "PVG", "date": "2026-08-07", "price": 2900, "airline": "ANA", "duration_h": 3.0},
]

_TRAINS = [
    {"route": "tokyo → osaka", "line": "shinkansen", "duration_min": 150, "price": 1400},
    {"route": "osaka → kyoto", "line": "local", "duration_min": 45, "price": 600},
    {"route": "kyoto → tokyo", "line": "shinkansen", "duration_min": 140, "price": 1400},
]

_WEATHER = {
    "tokyo": {"2026-08-01": "sunny", "2026-08-02": "rain", "2026-08-03": "sunny"},
    "osaka": {"2026-08-04": "cloudy", "2026-08-05": "sunny", "2026-08-06": "rain"},
    "kyoto": {"2026-08-05": "sunny", "2026-08-06": "sunny"},
}


# ── 城市 / 机场代码归一化 ────────────────────────────────────────────────────
# 真实 LLM 可能传 "Tokyo" / "tokyo" / "東京" / "NRT" / "TYO" —— 全归到 IATA 代码,
# 让 search_flights 能匹配到 mock 数据(PVG↔NRT)。

_CITY_TO_AIRPORT = {
    "tokyo": "NRT", "東京": "NRT", "tyo": "NRT", "nrt": "NRT", "hnd": "HND",
    "osaka": "KIX", "大阪": "KIX", "kix": "KIX", "itm": "ITM",
    "kyoto": "OSA", "京都": "OSA",
    "shanghai": "PVG", "上海": "PVG", "pvg": "PVG", "sha": "PVG",
    "beijing": "PEK", "北京": "PEK", "pek": "PEK",
}

# 真实 LLM 可能传各种日期格式 —— 归一到 YYYY-MM-DD。
_DATE_PATTERNS = [
    (r"(\d{4})-(\d{1,2})-(\d{1,2})", lambda m: f"{m[0]}-{int(m[1]):02d}-{int(m[2]):02d}"),
    (r"(\d{1,2})/(\d{1,2})/(\d{4})", lambda m: f"{m[2]}-{int(m[0]):02d}-{int(m[1]):02d}"),
    (r"(\d{1,2})-(\d{1,2})-(\d{4})", lambda m: f"{m[2]}-{int(m[0]):02d}-{int(m[1]):02d}"),
]


def _normalize_airport(value: str) -> str:
    """城市名 / 中文 / 混写 → IATA 代码。大小写不敏感。"""
    key = value.strip().lower()
    return _CITY_TO_AIRPORT.get(key, value.strip().upper())


def _normalize_date(value: str) -> str:
    """各种日期格式 → YYYY-MM-DD。匹配不上原样返回。"""
    s = value.strip()
    for pattern, formatter in _DATE_PATTERNS:
        match = re.fullmatch(pattern, s)
        if match:
            return formatter(match.groups())
    return s


def _normalize_route(value: str) -> str:
    """各种路由分隔符 → 标准空格+箭头格式。

    "Tokyo to Osaka" / "tokyo-osaka" / "Tokyo→Osaka" / "東京 大阪"
    → "tokyo osaka"
    """
    s = value.strip().lower()
    # 统一分隔符:各种箭头 / 横杠 → 普通空格
    s = re.sub(r"\s*(?:→|->|—|–)\s*", " ", s)
    s = re.sub(r"\s*-\s*", " ", s)
    # " to " 作为单词(前后有空格)才替换,避免吃掉 Tokyo 里的 "to"
    s = re.sub(r"\s+to\s+", " ", s)
    parts = [p for p in s.split() if p]
    return " ".join(parts)


# ── itinerary peer 工具 ──────────────────────────────────────────────────────


@tool(
    meta=ToolMeta(
        name="search_hotels",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        estimated_latency_ms=120,
        domain="travel",
    )
)
async def search_hotels(city: str, max_price_per_night: int = 30000) -> dict:
    """按城市 + 最高单价搜酒店。

    [TRIGGER] itinerary peer 拿到 prefs 后调,决定住哪家。
    [CONSTRAINT] 只读。
    """
    matches = [h for h in _HOTELS if h["area"] == city and h["price_per_night"] <= max_price_per_night]
    return {"city": city, "hotels": matches, "count": len(matches)}


@tool(
    meta=ToolMeta(
        name="get_weather",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        estimated_latency_ms=80,
        domain="travel",
    )
)
async def get_weather(city: str, dates: list[str]) -> dict:
    """查指定日期的天气。

    [TRIGGER] itinerary peer 排行程前调,雨天替换室外活动。
    [CONSTRAINT] 只读。
    """
    forecast = _WEATHER.get(city, {})
    return {
        "city": city,
        "forecast": [{"date": d, "weather": forecast.get(d, "sunny")} for d in dates],
    }


# ── restaurant peer 工具 ────────────────────────────────────────────────────


@tool(
    meta=ToolMeta(
        name="search_restaurants",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        estimated_latency_ms=100,
        domain="travel",
    )
)
async def search_restaurants(city: str, cuisine: str = "") -> dict:
    """按城市 + 菜系搜餐厅。

    [TRIGGER] restaurant peer 拿到 prefs 后调。
    [CONSTRAINT] 只读。
    """
    matches = [
        r for r in _RESTAURANTS
        if r["city"] == city and (not cuisine or r["cuisine"] == cuisine)
    ]
    return {"city": city, "restaurants": matches, "count": len(matches)}


@tool(
    meta=ToolMeta(
        name="book_restaurant",
        is_readonly=False,
        side_effect_level=SideEffectLevel.MEDIUM,
        estimated_latency_ms=200,
        domain="travel",
        resource_id="restaurant-booking",
        enforced_idempotent=True,
    )
)
async def book_restaurant(restaurant: str, date: str, party_size: int, idempotency_key: str = "") -> dict:
    """预订餐厅。

    [TRIGGER] restaurant peer 选定餐厅后调。
    [MUTEX] Tool 内自持 asyncio 锁(``restaurant-booking``)—— 忙时返回
        RESOURCE_BUSY,由上层 LLM 决定让路或稍后重试。
    [IDEMPOTENT] host 注入 idempotency_key,重放返回缓存结果。
    """
    busy = await _acquire_booking_lock()
    if busy is not None:
        return busy
    try:
        return {
            "booked": True,
            "restaurant": restaurant,
            "date": date,
            "party_size": party_size,
            "idempotency_key": idempotency_key,
        }
    finally:
        _BOOKING_LOCK.release()


# ── transport peer 工具 ──────────────────────────────────────────────────────


@tool(
    meta=ToolMeta(
        name="search_flights",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        estimated_latency_ms=150,
        domain="travel",
    )
)
async def search_flights(origin: str, dest: str, date: str) -> dict:
    """搜航班。

    [TRIGGER] transport peer 排跨国交通时调。
    [CONSTRAINT] 只读。origin/dest 接受城市名(如 "Tokyo")或 IATA 代码("NRT"),
    date 接受 "2026-08-01" / "8/1/2026" / "Aug 1 2026" 等格式。
    """
    o = _normalize_airport(origin)
    d = _normalize_airport(dest)
    dt = _normalize_date(date)
    # 精确匹配 → 返回;否则返回同 OD 所有日期(供 LLM 选最接近的)
    matches = [f for f in _FLIGHTS if f["from"] == o and f["to"] == d and f["date"] == dt]
    if matches:
        return {"origin": o, "dest": d, "date": dt, "flights": matches, "count": len(matches)}
    # 退化:返回同 OD 所有日期(让 LLM 看到可选航班,不要无限换格式重试)
    alt = [f for f in _FLIGHTS if f["from"] == o and f["to"] == d]
    if alt:
        return {
            "origin": o, "dest": d, "date": dt,
            "flights": [], "count": 0,
            "hint": f"没有 {dt} 的航班,但同路线有这些可选: {alt}",
        }
    # 再退化:列出所有航班(让 LLM 看到数据库里有什么路线)
    return {
        "origin": o, "dest": d, "date": dt,
        "flights": [], "count": 0,
        "hint": f"没有 {o}→{d} 航班。数据库里现有路线: {[(f['from'], f['to'], f['date']) for f in _FLIGHTS]}",
    }


@tool(
    meta=ToolMeta(
        name="search_trains",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        estimated_latency_ms=120,
        domain="travel",
    )
)
async def search_trains(route: str) -> dict:
    """搜城际火车。

    [TRIGGER] transport peer 排城市间交通时调。
    [CONSTRAINT] 只读。route 接受 "Tokyo to Osaka" / "tokyo-osaka" / "東京→大阪" 等。
    """
    norm = _normalize_route(route)
    # 双向匹配:"tokyo osaka" 命中 "tokyo → osaka" 和 "osaka → tokyo"
    parts = norm.split()
    matches = []
    if len(parts) >= 2:
        a, b = parts[0], parts[1]
        matches = [
            t for t in _TRAINS
            if (a in t["route"] and b in t["route"])
        ]
    if matches:
        return {"route": norm, "trains": matches, "count": len(matches)}
    return {
        "route": norm, "trains": [], "count": 0,
        "hint": f"没找到 '{norm}'。现有路线: {[t['route'] for t in _TRAINS]}",
    }


__all__ = [
    "book_restaurant",
    "get_weather",
    "search_flights",
    "search_hotels",
    "search_restaurants",
    "search_trains",
]
