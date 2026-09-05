"""playground 场景表：把 examples 里的业务场景集中成“选一下就能在网页跑”的形式。

每个场景返回一个已经组装好的 Agent 或 Workflow（二者都有 run/resume、都带 bus，
server 一视同仁）。模型统一走 env_llm：配了 OPENAI_API_KEY 用真实模型，
没配回落 ScriptedLlm 离线脚本，零配置、确定性地复现完整链路；
其中需要人拍板的地方统一用 wait_human 挂起，网页上点批准/拒绝后继续。
"""

from __future__ import annotations

import asyncio
import os

import src.runtime as _runtime_pkg
from src import Agent, Workflow, go, wait_human
from src.kernel import LlmReply, ToolCall
from src.runtime.context import TieredCompactionContext
from src.runtime.llm import ScriptedLlm, env_llm
from src.runtime.mcp import InProcessMCPServer, load_mcp_tools
from src.runtime.memory import InMemoryMemory
from src.runtime.skills import SkillRegistry
from src.runtime.tools import ToolRegistry


def _model(*script):
    """场景里的模型：真实环境变量优先，否则按离线脚本跑。"""
    return env_llm(ScriptedLlm(list(script)))


# ---------------------------------------------------------------- 01 问候点单
def _greeter():
    async def menu(drink, ctx):
        """查询某款饮品是否在售。"""
        return {"芋泥啵啵": "在售，18 元", "美式": "在售，12 元"}.get(drink, "菜单里没有")

    return Agent(
        name="greeter",
        model=_model(
            ToolCall("menu", {"drink": "芋泥啵啵"}),
            "有的，芋泥啵啵在售，18 元一杯，需要帮你下单吗？",
            "好嘞，已帮你下一杯芋泥啵啵，按你的偏好无糖去冰。",
        ),
        instruction="你是奶茶店助手，回答简洁。",
        tools=[menu],
    )


# ---------------------------------------------------------------- 02 代购砍价 + 下单审批
def _trader():
    price = {"v": 20}

    async def quote(ctx):
        """向商家询问当前价格。"""
        price["v"] -= 2
        return f"当前报价 {price['v']} 元"

    async def place_order(plan, ctx):
        return f"订单已下：{plan}"

    async def cancel(plan, ctx):
        return f"价格没谈拢，已放弃下单：{plan}"

    wf = Workflow()
    buyer = Agent(
        name="buyer",
        model=_model(
            ToolCall("quote", {}), ToolCall("quote", {}), "两轮砍价后谈到 14 元自取，准备下单。"
        ),
        instruction="你是代购助手，先砍价再申请下单。",
        tools=[quote],
        memory=InMemoryMemory(),
        bus=wf.bus,
    )  # 子 Agent 事件汇入同一总线

    async def approve(plan, ctx):
        if ctx.resume_value is None:
            return wait_human("代购谈到 14 元自取，批准下单吗？", {"plan": plan})
        target = "place_order" if ctx.resume_value.get("approved") else "cancel"
        return go(target, plan, decision=target)  # 写决策 + 带值给被选中的终态

    wf.add("buyer", buyer)
    wf.add("approve", approve)
    wf.add("place_order", place_order, terminal=True)
    wf.add("cancel", cancel, terminal=True)
    wf.edge("buyer", "approve")
    # 互斥分支用条件边声明拓扑：运行时只激活 decision 指向的那一个，
    # 另一个前驱已终态但边不活，会被 sweep_skipped 干净跳过。
    wf.branch(
        "approve",
        {"place_order": "place_order", "cancel": "cancel"},
        decide=lambda s: s.get("decision"),
    )
    wf.entry("buyer")
    return wf


# ---------------------------------------------------------------- 03 深度研究 + 上下文压缩
def _research():
    async def search(query, ctx):
        """检索资料。"""
        return f"关于「{query}」的检索结果：一条带数字的资料……"

    class ConstSummarizer:
        def __init__(self):
            self.times = 0

        async def chat(self, messages, tools=None, system=None):
            self.times += 1
            return LlmReply(text="（早期检索要点已压缩：市场规模、增速、主要玩家）")

    return Agent(
        name="researcher",
        model=_model(
            ToolCall("search", {"query": "市场规模"}),
            ToolCall("search", {"query": "年增速"}),
            ToolCall("search", {"query": "头部玩家"}),
            ToolCall("search", {"query": "政策风向"}),
            "报告：综合四轮检索，市场规模稳步增长，头部集中，政策友好……",
        ),
        instruction="你是行业研究员。",
        tools=[search],
        # 五级压缩：窗口够就不动，超了先机械缩短工具结果，再逐级摘要，只在摘要级花模型。
        # 摘要器同样跟着环境变量切真实模型。
        context=TieredCompactionContext(env_llm(ConstSummarizer()), capacity=6),
    )


# ---------------------------------------------------------------- 04 合规审计：并行核查 + 冻结审批
def _compliance():
    wf = Workflow()

    async def screen_suspicious(x, ctx):
        return {"flags": "发现快进快出交易"}

    async def screen_accounts(x, ctx):
        return {"links": "关联到 3 个同源账户"}

    async def synthesize(x, ctx):
        s = ctx.shared
        return f"综合判断：{s['flags']}；{s['links']}，建议冻结。"

    async def freeze(summary, ctx):
        if ctx.resume_value is None:
            return wait_human("批准冻结 A1、A2 两个账户吗？", {"accounts": ["A1", "A2"]})
        return go(
            "report",
            summary,
            decision="已冻结 A1、A2"
            if ctx.resume_value.get("approved")
            else "建议冻结，但本次未获批准",
        )

    async def report(summary, ctx):
        return f"{summary}｜处置：{ctx.shared['decision']}"

    wf.add("screen_suspicious", screen_suspicious)
    wf.add("screen_accounts", screen_accounts)
    wf.add("synthesize", synthesize, join="all")
    wf.add("freeze", freeze)
    wf.add("report", report, terminal=True)
    wf.entry("screen_suspicious", "screen_accounts")
    wf.edge("screen_suspicious", "synthesize")
    wf.edge("screen_accounts", "synthesize")
    wf.edge("synthesize", "freeze")
    wf.edge("freeze", "report")
    return wf


# ---------------------------------------------------------------- 05 代码侦探：MCP 工具 + 技能
async def _detective():
    repo = InProcessMCPServer("repo")
    repo.define("read_file", lambda a: f"【{a['file']} 的内容】", description="读文件")
    repo.define("grep", lambda a: f"在 {a['pattern']} 处命中", description="全文检索")
    repo.define("apply_patch", lambda a: "补丁已应用", description="修改代码")
    runs = {"n": 0}

    def run_test(a):
        runs["n"] += 1
        return "测试通过" if runs["n"] >= 2 else "1 个测试仍失败：边界没处理"

    repo.define("run_test", run_test, description="运行测试")

    registry = ToolRegistry()
    await load_mcp_tools(registry, repo)

    # 技能不是写死在代码里，而是从磁盘目录的 SKILL.md 加载（可渐进披露、可热插拔）。
    skills_dir = os.path.join(os.path.dirname(_runtime_pkg.__file__), "builtin_skills")
    skills = SkillRegistry()
    skills.load_dir(skills_dir)
    skill = skills.match("测试失败 排障 补丁 重跑")
    system = skills.apply_to_system(skill, "你是代码排障助手。")

    return Agent(
        name="detective",
        model=_model(
            ToolCall("read_file", {"file": "test_x.py"}),
            ToolCall("grep", {"pattern": "func_x"}),
            ToolCall("read_file", {"file": "x.py"}),
            ToolCall("apply_patch", {"change": "补边界"}),
            ToolCall("run_test", {}),
            ToolCall("apply_patch", {"change": "再补空值"}),
            ToolCall("run_test", {}),
            "定位到空值边界问题，两次修改后测试全部通过。",
        ),
        instruction=system,
        registry=registry,
    )


# ---------------------------------------------------------------- 06 行程规划：并行子 Agent
def _trip():
    wf = Workflow()

    def specialist(name, line):
        return Agent(name, model=_model(line), instruction=f"你负责{name}", bus=wf.bus)

    itinerary = specialist("itinerary", "第一天外滩、第二天迪士尼")
    dining = specialist("dining", "本帮菜晚餐已预留")
    traffic = specialist("traffic", "地铁 2 号线接驳，备打车方案")

    async def synth(parts, ctx):
        return "行程书已生成：\n- " + "\n- ".join(parts.values())

    wf.add("synth", synth, join="all", terminal=True)
    wf.add("itinerary", itinerary)
    wf.add("dining", dining)
    wf.add("traffic", traffic)
    wf.entry("itinerary", "dining", "traffic")
    wf.edge("itinerary", "synth")
    wf.edge("dining", "synth")
    wf.edge("traffic", "synth")
    return wf


# ---------------------------------------------------------------- 07 故障应急：并行委派 + 接力
def _aiops():
    wf = Workflow()

    # 只读的观测工具：诊断 Agent 有数据可查，而不是凭一句“看 CPU 曲线”瞎猜。
    async def cpu_metrics(ctx=None):
        """读取最近一小时的 CPU 曲线。"""
        return "12:00 35% → 12:10 92% → 12:20 93% → 12:30 91%（每十分钟打满一次）"

    async def error_log(ctx=None):
        """读取最近的错误日志。"""
        return "ERROR pool exhausted: 获取连接超时（等待 5000ms），近 1 小时共 37 次"

    def engineer(name, *script, tools=None):
        return Agent(
            name,
            model=_model(*script),
            instruction=f"你是{name}，先用工具查数据再下结论，两句话内给出结论。",
            tools=tools or [],
            bus=wf.bus,
        )

    diag_cpu = engineer(
        "diag_cpu",
        ToolCall("cpu_metrics", {}),
        "CPU 每十分钟周期性打满，疑似下游排队。",
        tools=[cpu_metrics],
    )
    diag_log = engineer(
        "diag_log",
        ToolCall("error_log", {}),
        "错误日志显示获取连接超时，连接池已耗尽。",
        tools=[error_log],
    )
    repairer = engineer("repairer", "已扩容连接池并对上游限流，服务恢复。")

    async def diagnose(x, ctx):
        cpu, log = await asyncio.gather(
            diag_cpu.delegate("看 CPU 曲线"), diag_log.delegate("看错误日志")
        )
        return go("decide", f"根因=连接池耗尽（{cpu}；{log}）")

    async def decide(root, ctx):
        # transfer：go 到同图的修复 Agent，无回边即不回头，root 作为它这一次的输入。
        return go("repairer", root)

    wf.add("diagnose", diagnose)
    wf.add("decide", decide)
    wf.add("repairer", repairer, terminal=True)
    wf.edge("diagnose", "decide")
    wf.entry("diagnose")
    return wf


# ---------------------------------------------------------------- 08 撰稿-审阅-修订：多 Agent + 条件分支
def _review_team():
    wf = Workflow()

    def author(name, line):
        return Agent(name, model=_model(line), instruction=f"你是{name}", bus=wf.bus)

    writer = author("writer", "初稿：本季度营收增长，建议扩张。")
    critic = author("critic", "审阅意见：缺少数据来源，需要补充后再定稿。")
    reviser = author("reviser", "修订稿：补充营收同比 +18% 的来源，结论不变。")

    async def judge(review, ctx):
        # 审阅不通过就去修订，通过就直接定稿——运行时按内容选边。
        target = "revise" if "补充" in str(review) else "finalize"
        return go(target, review, verdict=target, review=review)

    async def finalize(text, ctx):
        # 走到这里的输入：直接定稿时是审阅意见，经修订时是修订稿。
        return f"定稿完成：{text}"

    wf.add("writer", writer)
    wf.add("critic", critic)
    wf.add("judge", judge)
    wf.add("revise", reviser)
    # 汇聚点：judge 直接定稿、或 revise 修订后定稿，谁到都用它的输出，故 join=any。
    wf.add("finalize", finalize, join="any", terminal=True)
    wf.edge("writer", "critic")
    wf.edge("critic", "judge")
    wf.edge("revise", "finalize")
    # judge 的两个去向是互斥分支，运行时只激活一个，另一个被干净跳过。
    wf.branch(
        "judge", {"revise": "revise", "finalize": "finalize"}, decide=lambda s: s.get("verdict")
    )
    wf.entry("writer")
    return wf


# ---------------------------------------------------------------- 09 长期记忆：跨会话召回
async def _memory_regular():
    mem = InMemoryMemory()
    # 模拟前几次对话沉淀下来的长期记忆——它独立于本次上下文而存在。
    await mem.remember("用户点奶茶的偏好：默认无糖、去冰，回答尽量简短", tags=["偏好"])
    # think 前会用当前问题检索记忆并拼进 system，模型据此给出“记得你”的回答。
    return Agent(
        name="regular",
        model=_model(
            "好的，按你记忆中的偏好做了无糖去冰，已下单。",
            "记得呢——还是无糖去冰，这就再帮你下一单。",
        ),
        instruction="你是点单助手，要结合长期记忆里的偏好。",
        memory=mem,
    )


# 场景表：key -> 标题、简介、默认输入、构建器（可能是 async）。
SCENARIOS = [
    {
        "key": "01",
        "title": "01 问候点单",
        "default": "你们这儿有芋泥啵啵吗？",
        "desc": "最小的 Agent：想一步、调一次工具、再回答。",
        "build": _greeter,
        "is_async": False,
    },
    {
        "key": "02",
        "title": "02 代购砍价·下单审批",
        "default": "帮我买杯奶茶，尽量便宜",
        "desc": "多轮砍价后，动钱前在网页上挂起等你批准。",
        "build": _trader,
        "is_async": False,
    },
    {
        "key": "03",
        "title": "03 深度研究·上下文压缩",
        "default": "帮我研究新能源赛道",
        "desc": "连查多轮，超预算的早期检索被压成摘要。",
        "build": _research,
        "is_async": False,
    },
    {
        "key": "04",
        "title": "04 合规审计·并行+审批",
        "default": "审计账户 A1",
        "desc": "两路并行核查、汇合，冻结前挂起审批。",
        "build": _compliance,
        "is_async": False,
    },
    {
        "key": "05",
        "title": "05 代码侦探·MCP+技能",
        "default": "test_x 一直红，帮我修好",
        "desc": "MCP 工具在边界拉平，按技能指引失败再改到转绿。",
        "build": _detective,
        "is_async": True,
    },
    {
        "key": "06",
        "title": "06 行程规划·并行子 Agent",
        "default": "上海两日游",
        "desc": "同一波并行派三个专业 Agent，再汇合合成。",
        "build": _trip,
        "is_async": False,
    },
    {
        "key": "07",
        "title": "07 故障应急·委派与接力",
        "default": "订单服务延迟飙升",
        "desc": "并行委派诊断（call），再 go 到修复 Agent 接力（transfer）。",
        "build": _aiops,
        "is_async": False,
    },
    {
        "key": "08",
        "title": "08 撰稿-审阅-修订·多 Agent",
        "default": "写一份季度经营结论",
        "desc": "撰稿/审阅/修订三个 Agent，按审阅结果走条件分支再汇合定稿。",
        "build": _review_team,
        "is_async": False,
    },
    {
        "key": "09",
        "title": "09 长期记忆·跨会话召回",
        "default": "帮我点杯奶茶",
        "desc": "记忆独立于本次对话存在，think 前检索并注入，模型像记得你。",
        "build": _memory_regular,
        "is_async": True,
    },
]


def get_scenario(key: str):
    for s in SCENARIOS:
        if s["key"] == key:
            return s
    return None
