"""合规审计工具 —— extract_transactions + submit_to_regulator。

s1 用 ``wf.tool_step``（纯 tool），s2/s3 用 ``wf.llm_step``（真 LLM 标注/关联），
s4 是子 agent 调 ``submit_to_regulator``。本模块只提供:

  - ``extract_transactions``: s1，返回待审计的交易流水。
  - ``submit_to_regulator``: s4 子 agent 的写工具。对外提交 SAR 报告到监管
    系统，``enforced_idempotent`` 防崩溃重试时重复提交。

所有数据进程内 fake，示例离线可跑（前提是有 API key 给 s2/s3 的 LLM
标注用，或走 FakeLLM 脚本）。
"""

from __future__ import annotations

from prodagent import SideEffectLevel, ToolMeta, tool

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


# ── s1: 只读交易抽取 ────────────────────────────────────────────────────────────


@tool(
    meta=ToolMeta(
        name="extract_transactions",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        reversibility=1.0,
        estimated_latency_ms=100,
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


# ── s4 子 agent 的写工具: submit_to_regulator ──────────────────────────────────


@tool(
    meta=ToolMeta(
        name="submit_to_regulator",
        is_readonly=False,
        side_effect_level=SideEffectLevel.MEDIUM,
        reversibility=0.0,
        estimated_latency_ms=200,
        domain="compliance",
        resource_id="regulator-portal",
        enforced_idempotent=True,
    )
)
async def submit_to_regulator(
    sar_summary: str,
    suspicious_tx_ids: list[str],
    idempotency_key: str = "",
) -> dict:
    """提交可疑活动报告（SAR）到监管系统。

    [TRIGGER] s4 子 agent 综合完 s2/s3 的 LLM 标注后调。
    [SIDE_EFFECT] MEDIUM —— 对外提交，不可逆。
    [MUTEX] 持有 ``regulator-portal`` 资源锁。
    [IDEMPOTENT] host 注入 idempotency_key，重放返回缓存结果，防止重复提交。
    """
    return {
        "submitted": True,
        "sar_summary": sar_summary[:120],
        "suspicious_tx_ids": suspicious_tx_ids,
        "idempotency_key": idempotency_key,
    }


__all__ = ["extract_transactions", "submit_to_regulator"]
