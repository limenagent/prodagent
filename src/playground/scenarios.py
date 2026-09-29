"""Playground scenario catalog — metadata only.

The runnable graphs live in the root-level :mod:`examples` package; each
example module exposes a bilingual ``build(lang)``. This file is the single
table the web UI reads: the human-facing title, description and default input
in both languages, plus a pointer to the build and whether it is a coroutine.
There is no graph construction here, so the web UI and the standalone recipes
can never drift.

Run the playground from the repository root (``python -m src.playground``) so
``examples`` is importable. It is deliberately not bundled in the wheel: the
examples are source to read and run, not library code.
"""

from examples import (
    after_sales,
    aiops,
    blackboard,
    code_detective,
    compliance_audit,
    dating_chat,
    deep_research,
    greeter,
    long_term_memory,
    orchestrator,
    trader,
    write_review,
)

SCENARIOS = [
    {
        "key": "01",
        "title": "Greet & order",
        "desc": "The smallest agent: think once, call one tool, answer.",
        "default": "Do you have taro bubble tea?",
        "title_zh": "问候点单",
        "desc_zh": "最小的 Agent：想一步、调一次工具、再回答。",
        "default_zh": "你们这儿有芋泥啵啵吗？",
        "build": greeter.build,
        "is_async": False,
    },
    {
        "key": "02",
        "title": "Haggle · order approval",
        "desc": "After multi-round haggling, suspend before spending money and wait for your approval.",
        "default": "Buy me a bubble tea, as cheap as you can",
        "title_zh": "代购砍价·下单审批",
        "desc_zh": "多轮砍价后，动钱前在网页上挂起等你批准。",
        "default_zh": "帮我买杯奶茶，尽量便宜",
        "build": trader.build,
        "is_async": False,
    },
    {
        "key": "03",
        "title": "Deep research · context compaction",
        "desc": "Multiple search rounds; early results past capacity are compacted into summaries.",
        "default": "Research the new-energy sector for me",
        "title_zh": "深度研究·上下文压缩",
        "desc_zh": "连查多轮，超预算的早期检索被压成摘要。",
        "default_zh": "帮我研究新能源赛道",
        "build": deep_research.build,
        "is_async": False,
    },
    {
        "key": "04",
        "title": "Compliance audit · parallel + approval",
        "desc": "Two parallel checks converge; freezing suspends for approval.",
        "default": "Audit account A1",
        "title_zh": "合规审计·并行+审批",
        "desc_zh": "两路并行核查、汇合，冻结前挂起审批。",
        "default_zh": "审计账户 A1",
        "build": compliance_audit.build,
        "is_async": False,
    },
    {
        "key": "05",
        "title": "Code detective · MCP + skills",
        "desc": "MCP tools normalized at the boundary; a skill guides fail-fix-rerun until green.",
        "default": "test_x keeps failing; fix it for me",
        "title_zh": "代码侦探·MCP+技能",
        "desc_zh": "MCP 工具在边界拉平，按技能指引失败再改到转绿。",
        "default_zh": "test_x 一直红，帮我修好",
        "build": code_detective.build,
        "is_async": True,
    },
    {
        "key": "06",
        "title": "After-sales refund · supervisor & specialists",
        "desc": 'The supervisor\'s "tools" are other agents: dispatch billing/risk and answers come back; risk delegates again — three levels.',
        "default": "Order O-1234: customer charged but nothing shipped. Refund?",
        "title_zh": "售后退款·主管与专家",
        "desc_zh": "主管的“工具”是别的 Agent：派账单/风控专家拿回结论；风控再往下委派，长出三层。",
        "default_zh": "订单 O-1234：扣了款一直没发货，该不该退款？",
        "build": after_sales.build,
        "is_async": False,
    },
    {
        "key": "07",
        "title": "Incident response · delegate & handoff",
        "desc": "Parallel diagnosis via delegation (call), then `go` hands off to the repairer (transfer).",
        "default": "Order-service latency is spiking",
        "title_zh": "故障应急·委派与接力",
        "desc_zh": "并行委派诊断（call），再 go 到修复 Agent 接力（transfer）。",
        "default_zh": "订单服务延迟飙升",
        "build": aiops.build,
        "is_async": False,
    },
    {
        "key": "08",
        "title": "Write-review-revise · multi-agent",
        "desc": "Writer/critic/reviser agents; a conditional branch picks revise or finalize, then converge.",
        "default": "Write a quarterly business summary",
        "title_zh": "撰稿-审阅-修订·多 Agent",
        "desc_zh": "撰稿/审阅/修订三个 Agent，按审阅结果走条件分支再汇合定稿。",
        "default_zh": "写一份季度经营结论",
        "build": write_review.build,
        "is_async": False,
    },
    {
        "key": "09",
        "title": "Service audit · orchestrator-worker",
        "desc": "How many reviewers? The planner reads the live catalog; Send stamps one template copy per service, all in one wave.",
        "default": "Audit every deployed service before the release",
        "title_zh": "服务巡检·编排者-工人",
        "desc_zh": "几个审查员？规划者读线上目录，Send 按服务压出模板拷贝，同一波并发。",
        "default_zh": "发布前把已部署服务都巡检一遍",
        "build": orchestrator.build,
        "is_async": False,
    },
    {
        "key": "10",
        "title": "Proposal review · blackboard",
        "desc": "Experts only write to a shared board; the moderator reads it — not converged, re-arm everyone for another round.",
        "default": "Should we roll out the new pricing engine next week?",
        "title_zh": "方案评审·黑板",
        "desc_zh": "专家只往共享黑板写意见，裁判读板裁决；未收敛就重开一轮。",
        "default_zh": "新定价引擎下周上线，行吗？",
        "build": blackboard.build,
        "is_async": False,
    },
    {
        "key": "11",
        "title": "Agent blind date · memory vs hand-rolled window",
        "desc": "He truncates his own messages list and forwards raw tool dumps; she runs memory + five-level compaction. Same conversation, two context strategies — who still remembers the allergy?",
        "default": "Start the blind date",
        "title_zh": "Agent 相亲 · 记忆 对 手搓窗口",
        "desc_zh": "大牛手搓截断消息窗口、原文转发工具大结果；小美用长期记忆 + 五级压缩。同一场对话、两种上下文策略——谁还记得海鲜过敏？",
        "default_zh": "开始这场相亲",
        "build": dating_chat.build,
        "is_async": True,
    },
    {
        "key": "12",
        "title": "Long-term memory · cross-session recall",
        "desc": "Memory outlives the chat; retrieved and injected before think — the model remembers you.",
        "default": "Order me a bubble tea",
        "title_zh": "长期记忆 · 跨会话召回",
        "desc_zh": "记忆独立于本次对话存在，think 前检索并注入，模型像记得你。",
        "default_zh": "帮我点杯奶茶",
        "build": long_term_memory.build,
        "is_async": True,
    },
]


def get_scenario(key: str):
    return next((s for s in SCENARIOS if s["key"] == key), None)
