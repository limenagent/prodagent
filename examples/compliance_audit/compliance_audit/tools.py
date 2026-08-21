"""合规审计工具 —— extract_transactions + flag_suspicious + enrich_entity + submit_to_regulator。

所有工具都是独立可调用的，LLM Planner 在运行时动态生成 Plan DAG 来编排它们。

``flag_suspicious`` 和 ``enrich_entity`` 默认返回 canned 响应（和 AIOps 工具一样），
瞬间完成。设 ``USE_REAL_LLM_FOR_ANALYSIS=1`` 则走真实 LLM 路径。

``submit_to_regulator`` 为 HIGH 副作用 + 幂等，框架 ApprovalHooks 自动弹审批窗。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING, Any

from prodagent import SideEffectLevel, ToolMeta, tool

if TYPE_CHECKING:
    from prodagent import LLMClient

# 第十章"隔离优于共享": 锁是 Tool 实现者自己的职责,框架执行器不管。
# regulator-portal 资源用 Tool 内自持的 asyncio.Lock 串行化;忙时返回
# 结构化 resource_busy 反馈,由上层 LLM 决定让路还是稍后重试。
_REGULATOR_LOCK = asyncio.Lock()
_REGULATOR_LOCK_WAIT_S = 0.1  # 必须小于该工具 timeout_seconds（外层还有工具超时）


async def _acquire_regulator_lock() -> dict | None:
    """拿到 regulator-portal 锁返回 None;拿不到返回 LLM 可读的 RESOURCE_BUSY 反馈。"""
    try:
        await asyncio.wait_for(_REGULATOR_LOCK.acquire(), timeout=_REGULATOR_LOCK_WAIT_S)
        return None
    except TimeoutError:
        return {
            "error": True,
            "reason": "resource_busy",
            "code": "resource_busy",
            "error_severity": "yellow",
            "message": "Resource 'regulator-portal' is busy (held by another agent).",
            "hint": "Try an alternative task or retry later.",
        }

# ── 假交易流水 —— 5 条，混了正常 / 可疑 / 高危 ─────────────────────────────────

_TRANSACTIONS = [
    {
        "tx_id": "TX-1001",
        "amount": 250.00,
        "currency": "USD",
        "sender": "alice@corp.com",
        "receiver": "vendor-supplies-llc",
        "timestamp": "2026-07-22T09:14:00Z",
        "note": "monthly office supplies",
    },
    {
        "tx_id": "TX-1002",
        "amount": 48500.00,
        "currency": "USD",
        "sender": "finance@corp.com",
        "receiver": "shell-co-122-cayman",
        "timestamp": "2026-07-22T09:18:00Z",
        "note": "consulting fee — urgent",
    },
    {
        "tx_id": "TX-1003",
        "amount": 9800.00,
        "currency": "USD",
        "sender": "alice@corp.com",
        "receiver": "crypto-exchange-X",
        "timestamp": "2026-07-22T09:22:00Z",
        "note": "invoice 9912",
    },
    {
        "tx_id": "TX-1004",
        "amount": 120000.00,
        "currency": "USD",
        "sender": "finance@corp.com",
        "receiver": "shell-co-122-cayman",
        "timestamp": "2026-07-22T09:25:00Z",
        "note": "acquisition deposit",
    },
    {
        "tx_id": "TX-1005",
        "amount": 320.00,
        "currency": "USD",
        "sender": "bob@corp.com",
        "receiver": "saas-vendor-monthly",
        "timestamp": "2026-07-22T09:30:00Z",
        "note": "subscription renewal",
    },
]

_TX_COUNT = len(_TRANSACTIONS)

# ── 模块级 LLM 引用 —— agent 构造时注入 ─────────────────────────────────────────

_llm: LLMClient | None = None


def set_llm(llm: LLMClient | None) -> None:
    """注入 LLM 客户端，供 ``flag_suspicious`` / ``enrich_entity`` 内部调用。"""
    global _llm
    _llm = llm


# ── s1: 只读交易抽取 ────────────────────────────────────────────────────────────


@tool(
    meta=ToolMeta(
        name="extract_transactions",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        timeout_seconds=100 / 1_000,
        domain="compliance",
    )
)
async def extract_transactions() -> dict:
    """返回待审计的交易流水。

    [TRIGGER] 审计任务的第一个调用 —— 列出交易 + 元数据。
    [CONSTRAINT] 只读。
    """
    return {
        "count": _TX_COUNT,
        "transactions": _TRANSACTIONS,
    }


# ── flag_suspicious: LLM 驱动的可疑标注 ────────────────────────────────────────

# ── flag_suspicious / enrich_entity 的 canned 响应 ────────────────────────
# 和 AIOps 工具一样直接返回假数据，瞬间完成。设置 USE_REAL_LLM_FOR_ANALYSIS=1
# 才会走真实 LLM 路径。

_FLAGGED = {
    "flagged": [
        {"tx_id": "TX-1002", "reason": "大额汇至壳公司 shell-co-122-cayman，备注 urgent consulting fee 与业务实质不符", "risk": "high"},
        {"tx_id": "TX-1003", "reason": "9800 美元兑加密货币，接近 10000 申报阈值", "risk": "medium"},
        {"tx_id": "TX-1004", "reason": "12 万美元二次汇至同一壳公司，疑似拆分规避申报", "risk": "high"},
    ]
}

_ENTITIES = {
    "entities": [
        {"name": "shell-co-122-cayman", "tx_ids": ["TX-1002", "TX-1004"],
         "total_amount": 168500.0, "pattern": "同一壳公司两次大额收款，间隔 7 分钟"},
        {"name": "crypto-exchange-X", "tx_ids": ["TX-1003"],
         "total_amount": 9800.0, "pattern": "逼近 10000 申报阈值的加密兑换"},
    ]
}


@tool(
    meta=ToolMeta(
        name="flag_suspicious",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        timeout_seconds=50 / 1_000,
        domain="compliance",
    )
)
async def flag_suspicious(transactions: Any = None, **kwargs: Any) -> dict[str, Any]:
    """标注交易流水中每笔交易的可疑性。

    [TRIGGER] 拿到交易流水后，逐条判断可疑性。
    [CONSTRAINT] 只读。默认返回 canned 响应，设 USE_REAL_LLM_FOR_ANALYSIS=1 走 LLM。
    """
    if os.getenv("USE_REAL_LLM_FOR_ANALYSIS") == "1" and _llm is not None:
        payload = json.dumps(transactions, ensure_ascii=False, default=str) if transactions else "{}"
        response = await _llm.complete(
            [{"role": "user", "content": f"标注这段交易流水中每笔交易的可疑性:\n\n{payload}"}],
            system=(
                "你是反洗钱分析师。看交易流水，逐条判断可疑性。"
                "用紧凑 JSON 返回: {\"flagged\": [{\"tx_id\": \"...\", \"reason\": \"...\", \"risk\": \"low|medium|high\"}]}。"
            ),
        )
        content = response.content or ""
        return json.loads(content)
    return _FLAGGED


# ── enrich_entity: LLM 驱动的实体关联 ──────────────────────────────────────────

@tool(
    meta=ToolMeta(
        name="enrich_entity",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        timeout_seconds=50 / 1_000,
        domain="compliance",
    )
)
async def enrich_entity(transactions: Any = None, **kwargs: Any) -> dict[str, Any]:
    """关联交易流水中的实体，识别异常聚类。

    [TRIGGER] 拿到交易流水后，按收款方聚类识别模式。
    [CONSTRAINT] 只读。默认返回 canned 响应，设 USE_REAL_LLM_FOR_ANALYSIS=1 走 LLM。
    """
    if os.getenv("USE_REAL_LLM_FOR_ANALYSIS") == "1" and _llm is not None:
        payload = json.dumps(transactions, ensure_ascii=False, default=str) if transactions else "{}"
        response = await _llm.complete(
            [{"role": "user", "content": f"关联这段交易流水中的实体，识别异常聚类:\n\n{payload}"}],
            system=(
                "你是反洗钱实体关联分析师。看交易流水，按收款方聚类，识别同一主体的多笔交易、"
                "壳公司命名模式（shell-co-*）、以及发送方跨账户的行为。"
                "用紧凑 JSON 返回: {\"entities\": [{\"name\": \"...\", \"tx_ids\": [\"...\"], "
                "\"total_amount\": <n>, \"pattern\": \"...\"}]}。"
            ),
        )
        content = response.content or ""
        return json.loads(content)
    return _ENTITIES


# ── submit_to_regulator: 幂等写工具 ────────────────────────────────────────────


@tool(
    meta=ToolMeta(
        name="submit_to_regulator",
        is_readonly=False,
        side_effect_level=SideEffectLevel.HIGH,
        timeout_seconds=200 / 1_000,
        domain="compliance",
        resource_id="regulator-portal",
        enforced_idempotent=True,
    )
)
async def submit_to_regulator(
    sar_summary: str = "",
    suspicious_tx_ids: Any = None,
    flagged: Any = None,
    entities: Any = None,
    idempotency_key: str = "",
) -> dict:
    """提交可疑活动报告（SAR）到监管系统。

    [TRIGGER] 综合完可疑标注和实体关联后调。
    [SIDE_EFFECT] MEDIUM —— 对外提交，不可逆。
    [MUTEX] Tool 内自持 asyncio 锁(``regulator-portal``)—— 忙时返回
        RESOURCE_BUSY,由上层 LLM 决定让路或稍后重试。
    [IDEMPOTENT] host 注入 idempotency_key，重放返回缓存结果，防止重复提交。

    参数灵活：可以传 suspicious_tx_ids (list[str])，也可以传 flagged (list[dict])
    和 entities (list[dict])，工具会自动提取 tx_id。
    """
    busy = await _acquire_regulator_lock()
    if busy is not None:
        return busy
    try:
        # 从多种输入格式中提取 tx_id 列表
        tx_ids: list[str] = []
        if suspicious_tx_ids and isinstance(suspicious_tx_ids, list):
            for item in suspicious_tx_ids:
                if isinstance(item, str):
                    tx_ids.append(item)
                elif isinstance(item, dict):
                    tx_ids.append(str(item.get("tx_id", item)))
        if not tx_ids and flagged and isinstance(flagged, list):
            for item in flagged:
                if isinstance(item, dict):
                    tx_ids.append(str(item.get("tx_id", "")))

        # sar_summary 可能是 str、dict（模板引用了整个上游输出）或其他类型，统一转字符串
        if sar_summary and not isinstance(sar_summary, str):
            sar_summary = json.dumps(sar_summary, ensure_ascii=False, default=str)
        summary = sar_summary or (
            f"SAR based on {len(tx_ids)} suspicious transaction(s)"
            f"{' and entity analysis' if entities else ''}"
        )
        # 安全截断：确保 summary 是字符串
        if not isinstance(summary, str):
            summary = json.dumps(summary, ensure_ascii=False, default=str)
        return {
            "submitted": True,
            "sar_summary": summary[:120],
            "suspicious_tx_ids": tx_ids,
            "idempotency_key": idempotency_key,
        }
    finally:
        _REGULATOR_LOCK.release()


# ── draft_sar_for_review: 只读恢复工具（submit 被拒后的 fallback）──────────────
#
# submit_to_regulator 是 HIGH 写操作，执行前弹人工审批。人类 Reject 后，重规划
# 不应再尝试自动提交（会再次弹窗、再次被拒，陷死循环），而是转调这个 LOW/只读
# 工具：把已有的可疑标注 + 实体关联整理成 SAR 草稿，留待合规官人工复核。
# 对照第八章：传输失败 → 换协议 SCP（不同动作）；提交被拒 → 改草拟转复核（不同动作）。


@tool(
    meta=ToolMeta(
        name="draft_sar_for_review",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        timeout_seconds=100 / 1_000,
        domain="compliance",
    )
)
async def draft_sar_for_review(
    flagged: Any = None,
    entities: Any = None,
    reason: str = "",
) -> dict:
    """草拟可疑活动报告（SAR）留待合规官人工复核，不自动提交监管。

    [TRIGGER] submit_to_regulator 被人类审批拒绝后的恢复动作——不再尝试自动
        提交，而是复用已有的可疑标注和实体关联，整理成 SAR 草稿交合规官复核。
    [CONSTRAINT] 只读（不对外提交，不触发审批）。
    """
    tx_ids: list[str] = []
    if isinstance(flagged, list):
        for item in flagged:
            if isinstance(item, dict):
                tx_ids.append(str(item.get("tx_id", "")))
    summary = (
        reason
        or "SAR 草稿：自动提交被审批拒绝，转为留待合规官人工复核后决定是否上报"
    )
    if not isinstance(summary, str):
        summary = json.dumps(summary, ensure_ascii=False, default=str)
    return {
        "submitted": False,
        "drafted_for_review": True,
        "sar_summary": summary[:120],
        "flagged_tx_ids": tx_ids,
        "entity_count": len(entities) if isinstance(entities, list) else 0,
    }


__all__ = [
    "extract_transactions",
    "flag_suspicious",
    "enrich_entity",
    "submit_to_regulator",
    "draft_sar_for_review",
    "set_llm",
]
