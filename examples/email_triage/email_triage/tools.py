"""邮件分拣工具 —— 分级副作用驱动 HITL 路由。

三级，对应 ``SideEffectLevel``:

  LOW    — ``read_inbox``、``classify_email``: 只读，自动批准。
  MEDIUM — ``archive_email``、``mark_read``: 可逆写，自动批准但审计。
  HIGH   — ``delete_email``、``forward_external``: 不可逆或外部爆炸半径，
           **通过 ``ApprovalHooks`` 路由到人工审批**。``ToolMeta`` 上的
           ``SideEffectLevel.HIGH`` 标志是触发门禁的唯一声明。

所有工具操作一个进程内的假邮箱，让本示例离线可跑。
"""

from __future__ import annotations

from prodagent import SideEffectLevel, ToolMeta, tool

# ── 假邮箱 ───────────────────────────────────────────────────────────────────


class FakeMailbox:
    def __init__(self) -> None:
        self.emails: dict[str, dict[str, str]] = {
            "eml_001": {
                "from": "newsletter@tech.com",
                "subject": "Weekly digest",
                "body": "This week in tech: AI, chips, markets...",
                "folder": "inbox",
            },
            "eml_002": {
                "from": "boss@company.com",
                "subject": "Re: Q3 planning",
                "body": "Please review the attached deck before Friday.",
                "folder": "inbox",
            },
            "eml_003": {
                "from": "suspicious@phish.example",
                "subject": "Urgent: verify your account",
                "body": "Click here to verify your credentials immediately.",
                "folder": "inbox",
            },
            "eml_004": {
                "from": "noreply@github.com",
                "subject": "PR #42 merged",
                "body": "Your pull request was merged into main.",
                "folder": "inbox",
            },
        }
        self.archive_log: list[str] = []
        self.delete_log: list[str] = []
        self.forward_log: list[tuple[str, str]] = []


_MAILBOX = FakeMailbox()


# ── LOW: 只读工具 ────────────────────────────────────────────────────────────


@tool(
    meta=ToolMeta(
        name="read_inbox",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        reversibility=1.0,
        estimated_latency_ms=100,
        domain="email",
    )
)
async def read_inbox() -> dict:
    """列出当前收件箱里的所有邮件。

    [TRIGGER] 第一个调用 —— 返回邮件 ID + 主题 + 发件人。
    [CONSTRAINT] 只读。
    """
    items = [
        {"id": eid, "from": e["from"], "subject": e["subject"], "folder": e["folder"]}
        for eid, e in _MAILBOX.emails.items()
        if e["folder"] == "inbox"
    ]
    return {"count": len(items), "emails": items}


@tool(
    meta=ToolMeta(
        name="classify_email",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        reversibility=1.0,
        estimated_latency_ms=200,
        domain="email",
    )
)
async def classify_email(email_id: str) -> dict:
    """把一封邮件分类到某个类别 + 建议动作。

    [TRIGGER] read_inbox 返回的每封邮件调一次。
    [CONSTRAINT] 只读；不修改邮箱。

    Args:
        email_id: read_inbox 返回的邮件 ID。
    """
    eml = _MAILBOX.emails.get(email_id)
    if eml is None:
        return {"email_id": email_id, "error": f"no such email {email_id!r}"}

    subj = eml["subject"].lower()
    body = eml["body"].lower()
    if "verify" in subj and "click" in body:
        category, action = "phishing", "delete_email"
    elif "newsletter" in subj or "digest" in subj:
        category, action = "newsletter", "archive_email"
    elif "merged" in subj or "pull request" in subj:
        category, action = "notification", "archive_email"
    else:
        category, action = "action_needed", "keep"

    return {
        "email_id": email_id,
        "from": eml["from"],
        "subject": eml["subject"],
        "category": category,
        "suggested_action": action,
    }


# ── MEDIUM: 可逆写 —— 自动批准，审计 ─────────────────────────────────────────


@tool(
    meta=ToolMeta(
        name="archive_email",
        is_readonly=False,
        side_effect_level=SideEffectLevel.MEDIUM,
        reversibility=0.8,
        estimated_latency_ms=150,
        domain="email",
        resource_id="mailbox",
        enforced_idempotent=True,
    )
)
async def archive_email(email_id: str, idempotency_key: str = "") -> dict:
    """把邮件移到归档文件夹。

    [TRIGGER] classify_email 后，对 newsletter / notification。
    [CONSTRAINT] MEDIUM 副作用 —— 可逆（可以取消归档）。
    [MUTEX] 持有 ``mailbox`` 资源锁。

    Args:
        email_id: 要归档的邮件 ID。
        idempotency_key: 由 host 注入。
    """
    eml = _MAILBOX.emails.get(email_id)
    if eml is None:
        return {"email_id": email_id, "error": "not found"}
    eml["folder"] = "archive"
    _MAILBOX.archive_log.append(email_id)
    return {"email_id": email_id, "archived": True}


@tool(
    meta=ToolMeta(
        name="mark_read",
        is_readonly=False,
        side_effect_level=SideEffectLevel.MEDIUM,
        reversibility=0.9,
        estimated_latency_ms=100,
        domain="email",
        resource_id="mailbox",
        enforced_idempotent=True,
    )
)
async def mark_read(email_id: str, idempotency_key: str = "") -> dict:
    """把邮件标记为已读。

    [TRIGGER] 对用户会手动处理的 action_needed 邮件。
    [CONSTRAINT] MEDIUM 副作用 —— 轻易可逆。

    Args:
        email_id: 要标记已读的邮件 ID。
        idempotency_key: 由 host 注入。
    """
    return {"email_id": email_id, "marked_read": True}


# ── HIGH: 不可逆 / 外部爆炸半径 —— 需要 HITL ────────────────────────────────


@tool(
    meta=ToolMeta(
        name="delete_email",
        is_readonly=False,
        side_effect_level=SideEffectLevel.HIGH,
        reversibility=0.1,
        estimated_latency_ms=200,
        domain="email",
        resource_id="mailbox",
        enforced_idempotent=True,
    )
)
async def delete_email(email_id: str, idempotency_key: str = "") -> dict:
    """永久删除一封邮件。不可逆。

    [TRIGGER] classify_email 后，对 phishing / spam。
    [CONSTRAINT] HIGH 副作用 —— **需要人工审批**。
    [MUTEX] 持有 ``mailbox`` 资源锁。

    Args:
        email_id: 要删除的邮件 ID。
        idempotency_key: 由 host 注入。
    """
    eml = _MAILBOX.emails.get(email_id)
    if eml is None:
        return {"email_id": email_id, "error": "not found"}
    del _MAILBOX.emails[email_id]
    _MAILBOX.delete_log.append(email_id)
    return {"email_id": email_id, "deleted": True}


@tool(
    meta=ToolMeta(
        name="forward_external",
        is_readonly=False,
        side_effect_level=SideEffectLevel.HIGH,
        reversibility=0.2,
        estimated_latency_ms=300,
        domain="email",
        resource_id="smtp",
        enforced_idempotent=True,
    )
)
async def forward_external(email_id: str, to: str, idempotency_key: str = "") -> dict:
    """把邮件转发到外部地址。无法撤销。

    [TRIGGER] 用户明确要求转发时。
    [CONSTRAINT] HIGH 副作用 —— **需要人工审批**（外部收件人 = 数据外发）。

    Args:
        email_id: 要转发的邮件 ID。
        to: 外部收件人地址。
        idempotency_key: 由 host 注入。
    """
    _MAILBOX.forward_log.append((email_id, to))
    return {"email_id": email_id, "forwarded_to": to}


__all__ = [
    "read_inbox",
    "classify_email",
    "archive_email",
    "mark_read",
    "delete_email",
    "forward_external",
]
