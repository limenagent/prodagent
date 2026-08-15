"""奶茶代购工具 —— 提案 + 下单,按风险分级。

工具:
  - ``propose_order`` (LOW 只读) —— 提一个具体订单方案(饮品/单价/杯数/糖度/冰度)。
  - ``place_order`` (HIGH 不可逆) —— 下单付款;**需要人工审批**。

为什么 ``place_order`` 是 HIGH: 下单即扣款,真实金钱操作不可逆。框架默认
bundle 的 ApprovalHooks 把它路由到人工审批 —— playground 里弹 approve/reject
对话框,CLI 里走 stdin 提示。

数据流: LLM 是工具间的数据中介 —— ``propose_order`` 返回的 proposal 进
``run.messages``(tool_result),LLM 下一轮看到后把 drink/quantity/price 显式
传给 ``place_order``。``proposal_id`` 只是审计锚点(approve 的是哪个方案),
不承载业务数据。审批对话框直接显示 place_order 的参数(drink/quantity/price),
不需要从 checkpoint 反查。
"""

from __future__ import annotations

import asyncio
import uuid

from prodagent import SideEffectLevel, ToolMeta, tool


@tool(
    meta=ToolMeta(
        name="propose_order",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        estimated_latency_ms=20,
        domain="food_delivery",
    )
)
async def propose_order(
    drink: str,
    unit_price: float,
    quantity: int,
    sugar: str = "全糖",
    ice: str = "正常冰",
) -> dict:
    """提一个奶茶订单方案。只读,不下单。

    [TRIGGER] 每轮协商开始时调,提一个具体方案。
    [CONSTRAINT] 只读 —— 不改任何状态,只生成方案摘要。

    Args:
        drink: 饮品名(如"珍珠奶茶"、"杨枝甘露")。
        unit_price: 单杯价格(元)。
        quantity: 杯数。
        sugar: 糖度("全糖"/"七分糖"/"半糖"/"三分糖"/"无糖")。
        ice: 冰度("正常冰"/"少冰"/"微冰"/"去冰"/"热")。
    """
    return {
        "proposal_id": f"PROP-{uuid.uuid4().hex[:8].upper()}",
        "drink": drink,
        "unit_price": unit_price,
        "quantity": quantity,
        "sugar": sugar,
        "ice": ice,
        "total_price": unit_price * quantity,
    }


# ── HIGH: 不可逆 —— HITL 门禁 ────────────────────────────────────────────────

# 第十章"隔离优于共享": 锁是 Tool 实现者自己的职责,框架执行器不管。
# orders 资源用 Tool 内自持的 asyncio.Lock 串行化;忙时返回结构化
# resource_busy 反馈,由上层 LLM 决定让路还是稍后重试。
_ORDERS_LOCK = asyncio.Lock()
_ORDERS_LOCK_WAIT_S = 0.05  # 必须小于 estimated_latency_ms / 1000(外层还有工具超时)


async def _acquire_orders_lock() -> dict | None:
    """拿到 orders 锁返回 None;拿不到返回 LLM 可读的 RESOURCE_BUSY 反馈。"""
    try:
        await asyncio.wait_for(_ORDERS_LOCK.acquire(), timeout=_ORDERS_LOCK_WAIT_S)
        return None
    except TimeoutError:
        return {
            "error": True,
            "reason": "resource_busy",
            "code": "resource_busy",
            "error_severity": "yellow",
            "message": "Resource 'orders' is busy (held by another agent).",
            "hint": "Try an alternative task or retry later.",
        }


@tool(
    meta=ToolMeta(
        name="place_order",
        is_readonly=False,
        side_effect_level=SideEffectLevel.HIGH,
        estimated_latency_ms=100,
        domain="food_delivery",
        resource_id="orders",
        enforced_idempotent=True,
    )
)
async def place_order(
    proposal_id: str,
    drink: str,
    unit_price: float,
    quantity: int,
    sugar: str,
    ice: str,
    idempotency_key: str = "",
) -> dict:
    """下单付款 —— 真实扣款,不可逆。

    [TRIGGER] 用户明确同意("可以"/"下单"/"确认")后调。
    [CONSTRAINT] HIGH —— 框架会自动 suspend 等人工审批,调用方直接调用即可,
    不要在文本里请求确认。
    [MUTEX] Tool 内自持 asyncio 锁(``orders``)—— 忙时返回 RESOURCE_BUSY,
    由上层 LLM 决定让路或稍后重试。

    Args:
        proposal_id: 要下单的方案 ID(``propose_order`` 返回的)—— 审计锚点,
            approve 的是哪个方案。LLM 从上一轮 tool_result 读出来传进来。
        drink: 饮品名(从 proposal 复述,审批对话框直接显示)。
        unit_price: 单杯价格(元)。
        quantity: 杯数。
        sugar: 糖度。
        ice: 冰度。
        idempotency_key: 由 host 注入。
    """
    busy = await _acquire_orders_lock()
    if busy is not None:
        return busy
    try:
        return {
            "order_id": f"ORD-{uuid.uuid4().hex[:8].upper()}",
            "proposal_id": proposal_id,
            "placed": True,
            "drink": drink,
            "unit_price": unit_price,
            "quantity": quantity,
            "sugar": sugar,
            "ice": ice,
            "total_price": unit_price * quantity,
        }
    finally:
        _ORDERS_LOCK.release()


__all__ = [
    "propose_order",
    "place_order",
]
