"""Playground scenario table: the business scenarios from `examples/`, gathered
into pick-one-and-run-it-in-the-browser form.

Each scenario returns a fully assembled Agent or Workflow (both have
run/resume, both carry a bus — the server treats them alike). Models uniformly
go through env_llm: with OPENAI_API_KEY set a real model is used, otherwise it
falls back to ScriptedLlm offline scripts — zero config, deterministic, the
full chain reproduces. Every place a human must decide uses wait_human, so the
page suspends there and continues after Approve/Reject.

Every builder takes a `lang` ("en" | "zh") so the scripted dialog, the model
instruction, and the approval question all follow the UI language. The one
exception: the skill-match query in scenario 05 stays Chinese in both
languages — it word-matches the bundled Chinese SKILL.md.
"""

from __future__ import annotations

import asyncio
import os

import src.runtime as _runtime_pkg
from src import Agent, Bus, Workflow, append, go, last, send, wait_human
from src.kernel import Goto, LlmReply, Outcome, ToolCall
from src.runtime.context import TieredCompactionContext
from src.runtime.llm import ScriptedLlm, env_llm
from src.runtime.mcp import InProcessMCPServer, load_mcp_tools
from src.runtime.memory import InMemoryMemory
from src.runtime.plan_first import parse_numbered_list
from src.runtime.skills import SkillRegistry
from src.runtime.tools import ToolRegistry


def _model(*script):
    """A scenario's model: real env vars win, otherwise run the offline script."""
    return env_llm(ScriptedLlm(list(script)))


# ---------------------------------------------------------------- 01 greet & order
def _greeter(lang: str = "en"):
    if lang == "zh":

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

    async def menu(drink, ctx):
        """Check whether a drink is on the menu."""
        return {
            "taro-bubble-tea": "in stock, ¥18",
            "americano": "in stock, ¥12",
        }.get(drink, "not on the menu")

    return Agent(
        name="greeter",
        model=_model(
            ToolCall("menu", {"drink": "taro-bubble-tea"}),
            "Yes — taro bubble tea is in stock, ¥18 a cup. Want me to order one?",
            "Done! One taro bubble tea for you, no sugar, less ice, as usual.",
        ),
        instruction="You are a bubble-tea shop assistant; keep answers short.",
        tools=[menu],
    )


# ---------------------------------------------------------------- 02 haggle + order approval
def _trader(lang: str = "en"):
    price = {"v": 20}

    async def quote(ctx):
        """Ask the seller for the current price."""
        price["v"] -= 2
        return f"current quote: ¥{price['v']}" if lang == "en" else f"当前报价 {price['v']} 元"

    async def place_order(plan, ctx):
        return f"Order placed: {plan}" if lang == "en" else f"订单已下：{plan}"

    async def cancel(plan, ctx):
        return (
            f"No deal on price; order abandoned: {plan}"
            if lang == "en"
            else f"价格没谈拢，已放弃下单：{plan}"
        )

    wf = Workflow()
    buyer = Agent(
        name="buyer",
        model=_model(
            *(
                [
                    ToolCall("quote", {}),
                    ToolCall("quote", {}),
                    "After two rounds of haggling: ¥14, self pickup. Ready to order.",
                ]
                if lang == "en"
                else [
                    ToolCall("quote", {}),
                    ToolCall("quote", {}),
                    "两轮砍价后谈到 14 元自取，准备下单。",
                ]
            )
        ),
        instruction=(
            "You are a purchasing agent: haggle first, then request order approval."
            if lang == "en"
            else "你是代购助手，先砍价再申请下单。"
        ),
        tools=[quote],
        memory=InMemoryMemory(),
        bus=wf.bus,
    )  # the child agent's events feed the same bus

    async def approve(plan, ctx):
        if ctx.resume_value is None:
            question = (
                "Haggled down to ¥14, self pickup. Approve the order?"
                if lang == "en"
                else "代购谈到 14 元自取，批准下单吗？"
            )
            return wait_human(question, {"plan": plan})
        target = "place_order" if ctx.resume_value.get("approved") else "cancel"
        # Record the decision + carry the value to the chosen terminal node.
        return go(target, plan, decision=target)

    wf.add("buyer", buyer)
    wf.add("approve", approve)
    wf.add("place_order", place_order, terminal=True)
    wf.add("cancel", cancel, terminal=True)
    wf.edge("buyer", "approve")
    # Mutually exclusive branches declared as conditional edges: the runtime
    # activates only the one `decision` points at; the other predecessor is
    # already terminal but its edge is inactive, so sweep_skipped skips it clean.
    wf.branch(
        "approve",
        {"place_order": "place_order", "cancel": "cancel"},
        decide=lambda s: s.get("decision"),
    )
    wf.entry("buyer")
    return wf


# ---------------------------------------------------------------- 03 deep research + context compaction
def _research(lang: str = "en"):
    async def search(query, ctx):
        """Search for material."""
        return (
            f"Search results for '{query}': one data-rich source…"
            if lang == "en"
            else f"关于「{query}」的检索结果：一条带数字的资料……"
        )

    class ConstSummarizer:
        def __init__(self):
            self.times = 0

        async def chat(self, messages, tools=None, system=None):
            self.times += 1
            return LlmReply(
                text=(
                    "(early searches compacted: market size, growth rate, key players)"
                    if lang == "en"
                    else "（早期检索要点已压缩：市场规模、增速、主要玩家）"
                )
            )

    queries = (
        ["market size", "growth rate", "top players", "policy outlook"]
        if lang == "en"
        else ["市场规模", "年增速", "头部玩家", "政策风向"]
    )
    final = (
        "Report: across four rounds of search, the market grows steadily, "
        "the top players concentrate, and policy is friendly…"
        if lang == "en"
        else "报告：综合四轮检索，市场规模稳步增长，头部集中，政策友好……"
    )

    return Agent(
        name="researcher",
        model=_model(
            ToolCall("search", {"query": queries[0]}),
            ToolCall("search", {"query": queries[1]}),
            ToolCall("search", {"query": queries[2]}),
            ToolCall("search", {"query": queries[3]}),
            final,
        ),
        instruction="You are an industry researcher." if lang == "en" else "你是行业研究员。",
        tools=[search],
        # Five-level compaction: untouched while it fits; past capacity, tool
        # results are mechanically shortened first, then summarized level by
        # level — only the summary levels spend model calls. The summarizer
        # follows env vars to a real model too.
        context=TieredCompactionContext(env_llm(ConstSummarizer()), capacity=6),
    )


# ---------------------------------------------------------------- 04 compliance audit: parallel checks + freeze approval
def _compliance(lang: str = "en"):
    wf = Workflow()

    async def screen_suspicious(x, ctx):
        return (
            {"flags": "fast-in fast-out transfers detected"}
            if lang == "en"
            else {"flags": "发现快进快出交易"}
        )

    async def screen_accounts(x, ctx):
        return (
            {"links": "linked to 3 accounts of the same origin"}
            if lang == "en"
            else {"links": "关联到 3 个同源账户"}
        )

    async def synthesize(x, ctx):
        s = ctx.shared
        return (
            f"Synthesis: {s['flags']}; {s['links']}. Recommend freezing."
            if lang == "en"
            else f"综合判断：{s['flags']}；{s['links']}，建议冻结。"
        )

    async def freeze(summary, ctx):
        if ctx.resume_value is None:
            question = (
                "Freeze accounts A1 and A2?" if lang == "en" else "批准冻结 A1、A2 两个账户吗？"
            )
            return wait_human(question, {"accounts": ["A1", "A2"]})
        if ctx.resume_value.get("approved"):
            decision = "froze A1 & A2" if lang == "en" else "已冻结 A1、A2"
        else:
            decision = (
                "freeze recommended but not approved this time"
                if lang == "en"
                else "建议冻结，但本次未获批准"
            )
        return go("report", summary, decision=decision)

    async def report(summary, ctx):
        return (
            f"{summary} | action taken: {ctx.shared['decision']}"
            if lang == "en"
            else f"{summary}｜处置：{ctx.shared['decision']}"
        )

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


# ---------------------------------------------------------------- 05 code detective: MCP tools + skills
async def _detective(lang: str = "en"):
    repo = InProcessMCPServer("repo")
    if lang == "en":
        repo.define("read_file", lambda a: f"[contents of {a['file']}]", description="read a file")
        repo.define("grep", lambda a: f"hits at {a['pattern']}", description="full-text search")
        repo.define("apply_patch", lambda a: "patch applied", description="modify code")
    else:
        repo.define("read_file", lambda a: f"【{a['file']} 的内容】", description="读文件")
        repo.define("grep", lambda a: f"在 {a['pattern']} 处命中", description="全文检索")
        repo.define("apply_patch", lambda a: "补丁已应用", description="修改代码")
    runs = {"n": 0}

    def run_test(a):
        runs["n"] += 1
        if lang == "en":
            return "tests pass" if runs["n"] >= 2 else "1 test still failing: boundary not handled"
        return "测试通过" if runs["n"] >= 2 else "1 个测试仍失败：边界没处理"

    desc_test = "run the tests" if lang == "en" else "运行测试"
    repo.define("run_test", run_test, description=desc_test)

    registry = ToolRegistry()
    await load_mcp_tools(registry, repo)

    # Skills are not hard-coded: they load from SKILL.md files on disk
    # (progressively disclosable, hot-swappable). The bundled skill is written
    # in Chinese, so the match query stays Chinese in both UI languages —
    # word-overlap matching would miss it in English.
    skills_dir = os.path.join(os.path.dirname(_runtime_pkg.__file__), "builtin_skills")
    skills = SkillRegistry()
    skills.load_dir(skills_dir)
    skill = skills.match("测试失败 排障 补丁 重跑")
    base_system = "You are a code-debugging assistant." if lang == "en" else "你是代码排障助手。"
    system = skills.apply_to_system(skill, base_system)

    if lang == "en":
        script = [
            ToolCall("read_file", {"file": "test_x.py"}),
            ToolCall("grep", {"pattern": "func_x"}),
            ToolCall("read_file", {"file": "x.py"}),
            ToolCall("apply_patch", {"change": "guard the boundary"}),
            ToolCall("run_test", {}),
            ToolCall("apply_patch", {"change": "also guard the None case"}),
            ToolCall("run_test", {}),
            "Found a None-boundary bug; after two fixes all tests pass.",
        ]
    else:
        script = [
            ToolCall("read_file", {"file": "test_x.py"}),
            ToolCall("grep", {"pattern": "func_x"}),
            ToolCall("read_file", {"file": "x.py"}),
            ToolCall("apply_patch", {"change": "补边界"}),
            ToolCall("run_test", {}),
            ToolCall("apply_patch", {"change": "再补空值"}),
            ToolCall("run_test", {}),
            "定位到空值边界问题，两次修改后测试全部通过。",
        ]

    return Agent(
        name="detective",
        model=_model(*script),
        instruction=system,
        registry=registry,
    )


# ---------------------------------------------------------------- 06 after-sales refund: supervisor + specialist delegation
def _after_sales(lang: str = "en"):
    # One bus for the whole delegation tree, so the page shows every level.
    bus = Bus()

    async def query_invoice(order_id, ctx):
        """Read the invoice and payment status of an order."""
        return (
            f"{order_id}: charged ¥399 on Sep 2, shipment never dispatched"
            if lang == "en"
            else f"{order_id}: 9 月 2 日已扣款 399 元，商品始终未发货"
        )

    async def query_blacklist(order_id, ctx):
        """Check whether an order touches any blacklisted account."""
        return (
            f"{order_id}: buyer clean, no blacklist hits"
            if lang == "en"
            else f"{order_id}: 买家干净，无黑名单命中"
        )

    async def query_related(order_id, ctx):
        """List accounts related to the buyer of an order."""
        return (
            f"{order_id}: 1 related account, dormant, no fraud record"
            if lang == "en"
            else f"{order_id}: 关联账号 1 个，长期沉寂，无欺诈记录"
        )

    def expert(name, *script, instruction, tools=None, teammates=None):
        # No bus passed: assembly shares the supervisor's bus down the whole
        # delegation tree, so every level lands on the timeline.
        return Agent(
            name,
            model=_model(*script),
            instruction=instruction,
            tools=tools or [],
            teammates=teammates,
        )

    if lang == "en":
        related = expert(
            "related",
            ToolCall("query_related", {"order_id": "O-1234"}),
            "One related account, dormant for 2 years, no fraud record.",
            instruction="You analyze accounts related to a buyer; answer in one sentence.",
            tools=[query_related],
        )
        risk = expert(
            "risk",
            ToolCall("query_blacklist", {"order_id": "O-1234"}),
            ToolCall("related", {"task": "check accounts related to order O-1234"}),
            "Blacklist clean; the one related account is dormant — risk is low.",
            instruction="You judge fraud and credit risk; check the data before concluding.",
            tools=[query_blacklist],
            teammates=[related],
        )
        billing = expert(
            "billing",
            ToolCall("query_invoice", {"order_id": "O-1234"}),
            "Charged ¥399 on Sep 2 and the shipment never went out; refund due in full.",
            instruction="You answer billing facts only: invoices, payments, refunds.",
            tools=[query_invoice],
        )
        sup_script = [
            ToolCall("billing", {"task": "gather the billing facts of order O-1234"}),
            ToolCall("risk", {"task": "assess the fraud risk of order O-1234"}),
            "Billing confirms charged-but-unshipped and risk is low: approve a full ¥399 refund.",
        ]
        sup_instruction = (
            "You are the after-sales supervisor. You never execute yourself: "
            "dispatch the right specialist, wait for the answer, then decide."
        )
    else:
        related = expert(
            "related",
            ToolCall("query_related", {"order_id": "O-1234"}),
            "关联账号仅 1 个，已沉寂 2 年，无欺诈记录。",
            instruction="你分析买家的关联账号，一句话给出结论。",
            tools=[query_related],
        )
        risk = expert(
            "risk",
            ToolCall("query_blacklist", {"order_id": "O-1234"}),
            ToolCall("related", {"task": "核查订单 O-1234 买家的关联账号"}),
            "黑名单干净，唯一关联账号已沉寂——风险低。",
            instruction="你判断欺诈与信用风险，先查数据再下结论。",
            tools=[query_blacklist],
            teammates=[related],
        )
        billing = expert(
            "billing",
            ToolCall("query_invoice", {"order_id": "O-1234"}),
            "9 月 2 日扣款 399 元且始终未发货，应全额退款。",
            instruction="你只回答账单事实：发票、支付、退款。",
            tools=[query_invoice],
        )
        sup_script = [
            ToolCall("billing", {"task": "查订单 O-1234 的账单事实"}),
            ToolCall("risk", {"task": "评估订单 O-1234 的欺诈风险"}),
            "账单确认扣款未发货、风险低：批准全额退款 399 元。",
        ]
        sup_instruction = "你是售后主管，自己不执行：挑对专家、等结果、再做决定。"

    # The supervisor's "tools" are the specialists above — dispatch, answer
    # comes back, dispatch the next, then decide.
    return Agent(
        "supervisor",
        model=_model(*sup_script),
        instruction=sup_instruction,
        teammates=[billing, risk],
        bus=bus,
    )


# ---------------------------------------------------------------- 07 incident response: parallel delegation + handoff
def _aiops(lang: str = "en"):
    wf = Workflow()

    # Read-only observability tools: the diagnosing agents have data to check,
    # instead of guessing from a vague "look at the CPU curve".
    async def cpu_metrics(ctx=None):
        """Read the last hour's CPU curve."""
        return (
            "12:00 35% → 12:10 92% → 12:20 93% → 12:30 91% (spiking every ten minutes)"
            if lang == "en"
            else "12:00 35% → 12:10 92% → 12:20 93% → 12:30 91%（每十分钟打满一次）"
        )

    async def error_log(ctx=None):
        """Read the recent error log."""
        return (
            "ERROR pool exhausted: connection wait timed out (5000ms), 37 times in the last hour"
            if lang == "en"
            else "ERROR pool exhausted: 获取连接超时（等待 5000ms），近 1 小时共 37 次"
        )

    def engineer(name, *script, tools=None):
        instruction = (
            f"You are {name}: check the data with tools before concluding, in two sentences."
            if lang == "en"
            else f"你是{name}，先用工具查数据再下结论，两句话内给出结论。"
        )
        return Agent(
            name,
            model=_model(*script),
            instruction=instruction,
            tools=tools or [],
            bus=wf.bus,
        )

    if lang == "en":
        diag_cpu = engineer(
            "diag_cpu",
            ToolCall("cpu_metrics", {}),
            "CPU saturates periodically every ten minutes; suspect queuing downstream.",
            tools=[cpu_metrics],
        )
        diag_log = engineer(
            "diag_log",
            ToolCall("error_log", {}),
            "Error log shows connection-wait timeouts; the pool is exhausted.",
            tools=[error_log],
        )
        repairer = engineer(
            "repairer", "Connection pool enlarged and upstream throttled; service recovered."
        )
        ask_cpu, ask_log = "check the CPU curve", "check the error log"
        root_fmt = "root cause = pool exhaustion ({cpu}; {log})"
    else:
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
        ask_cpu, ask_log = "看 CPU 曲线", "看错误日志"
        root_fmt = "根因=连接池耗尽（{cpu}；{log}）"

    async def diagnose(x, ctx):
        cpu, log = await asyncio.gather(diag_cpu.delegate(ask_cpu), diag_log.delegate(ask_log))
        return go("decide", root_fmt.format(cpu=cpu, log=log))

    async def decide(root, ctx):
        # transfer: `go` to the repair agent in the same graph; no return edge
        # means no coming back — root becomes its input for this run.
        return go("repairer", root)

    wf.add("diagnose", diagnose)
    wf.add("decide", decide)
    wf.add("repairer", repairer, terminal=True)
    wf.edge("diagnose", "decide")
    wf.entry("diagnose")
    return wf


# ---------------------------------------------------------------- 08 write-review-revise: multi-agent + conditional branch
def _review_team(lang: str = "en"):
    wf = Workflow()

    def author(name, line):
        instruction = f"You are {name}." if lang == "en" else f"你是{name}"
        return Agent(name, model=_model(line), instruction=instruction, bus=wf.bus)

    if lang == "en":
        writer = author("writer", "Draft: revenue grew this quarter; recommend expanding.")
        critic = author("critic", "Review: lacks data sources — revise before finalizing.")
        reviser = author(
            "reviser", "Revision: added the source for +18% YoY revenue; conclusion unchanged."
        )
    else:
        writer = author("writer", "初稿：本季度营收增长，建议扩张。")
        critic = author("critic", "审阅意见：缺少数据来源，需要补充后再定稿。")
        reviser = author("reviser", "修订稿：补充营收同比 +18% 的来源，结论不变。")

    async def judge(review, ctx):
        # A failing review goes to revision, a passing one straight to finalize
        # — the runtime picks the side by content. (The keyword matches the
        # critic's scripted line in the current language.)
        if lang == "en":
            target = "revise" if "lacks" in str(review).lower() else "finalize"
        else:
            target = "revise" if "补充" in str(review) else "finalize"
        return go(target, review, verdict=target, review=review)

    async def finalize(text, ctx):
        # Input here: the review comment when finalized directly, or the
        # revised draft when it went through revision.
        return f"Finalized: {text}" if lang == "en" else f"定稿完成：{text}"

    wf.add("writer", writer)
    wf.add("critic", critic)
    wf.add("judge", judge)
    wf.add("revise", reviser)
    # Convergence point: either judge finalizes directly or revise does after
    # rewriting — whoever arrives supplies the output, hence join="any".
    wf.add("finalize", finalize, join="any", terminal=True)
    wf.edge("writer", "critic")
    wf.edge("critic", "judge")
    wf.edge("revise", "finalize")
    # judge's two destinations are mutually exclusive branches: the runtime
    # activates one; the other is skipped clean.
    wf.branch(
        "judge", {"revise": "revise", "finalize": "finalize"}, decide=lambda s: s.get("verdict")
    )
    wf.entry("writer")
    return wf


# ---------------------------------------------------------------- 09 service audit: orchestrator-worker, runtime fan-out
def _orchestrator(lang: str = "en"):
    async def catalog(ctx=None):
        """List the services deployed in this environment."""
        return (
            "deployed services: auth, payment, search, notification"
            if lang == "en"
            else "已部署服务：auth、payment、search、notification"
        )

    if lang == "en":
        plan_text = "1. audit auth\n2. audit payment\n3. audit search\n4. audit notification"
        plan_instruction = (
            "You plan a service audit: call the catalog, then output one "
            "numbered line per service, 'N. audit <service>'."
        )
        findings = {
            "auth": "p95 41ms, errors 0.0% — pass",
            "payment": "p95 188ms, errors 0.3% — pass, watch item",
            "search": "p95 320ms, errors 2.1% — FAIL: retry storm from a cold cache",
            "notification": "p95 65ms, errors 0.1% — pass",
        }
        synth_line = (
            "3 of 4 services pass; search fails on a retry storm — "
            "roll back the cache change before the release."
        )
        synth_instruction = "You write the audit summary in two sentences."
    else:
        plan_text = "1. 审计 auth\n2. 审计 payment\n3. 审计 search\n4. 审计 notification"
        plan_instruction = (
            "你规划一次服务巡检：先调目录工具，再按「N. 审计 <服务>」每服务一行编号输出。"
        )
        findings = {
            "auth": "p95 41ms，错误率 0.0% —— 通过",
            "payment": "p95 188ms，错误率 0.3% —— 通过，需关注",
            "search": "p95 320ms，错误率 2.1% —— 不通过：冷缓存引发重试风暴",
            "notification": "p95 65ms，错误率 0.1% —— 通过",
        }
        synth_line = "4 个服务 3 个通过；search 因重试风暴不通过——发布前先回滚缓存变更。"
        synth_instruction = "你用两句话写出巡检结论。"

    wf = Workflow()
    planner = Agent(
        "planner",
        model=_model(ToolCall("catalog", {}), plan_text),
        instruction=plan_instruction,
        tools=[catalog],
        bus=wf.bus,
    )
    synth = Agent("synth", model=_model(synth_line), instruction=synth_instruction, bus=wf.bus)

    async def dispatch(plan, ctx):
        # Each plan line becomes one Send: a fresh copy of the reviewer
        # template, keyed so results map back to their service.
        steps = parse_numbered_list(plan)
        return [send("reviewer", step, key=step["id"]) for step in steps]

    async def reviewer(step, ctx):
        service = step["instruction"].split(maxsplit=1)[1]
        return f"{service}: {findings[service]}"

    wf.add("planner", planner)
    wf.add("dispatch", dispatch)
    wf.add("reviewer", reviewer, template=True)  # copies stamped at runtime
    wf.add("synth", synth, terminal=True)  # join="all": waits for every copy
    wf.edge("planner", "dispatch")
    wf.edge("reviewer", "synth")
    wf.entry("planner")
    return wf


# ---------------------------------------------------------------- 10 proposal review: blackboard, multi-round consensus
def _blackboard(lang: str = "en"):
    if lang == "en":
        scripts = {
            "finance": [
                "Objection: the budget doubles this quarter's cap — needs a phased rollout.",
                "Agreed: phased rollout keeps spend inside this quarter's cap.",
            ],
            "legal": [
                "Objection: the EU data-processing clause is missing from the contract.",
                "Agreed: the updated contract adds the EU data-processing clause.",
            ],
            "ops": [
                "Concern: no maintenance window is scheduled for the rollout.",
                "Agreed: the Sunday 02:00 window works for operations.",
            ],
        }
        agree = "Agreed"
        verdict_ok = "Consensus: proceed with the phased rollout."
        verdict_cap = (
            "Round cap reached without full consensus; proceeding with the phased rollout."
        )
        final_note = "{n} opinions were written along the way."
    else:
        scripts = {
            "finance": [
                "反对：预算翻倍超出本季度上限——需要分期上线。",
                "同意：分期上线后预算控制在本季度上限内。",
            ],
            "legal": [
                "反对：合同缺少欧盟数据处理条款。",
                "同意：更新后的合同已补充欧盟数据处理条款。",
            ],
            "ops": [
                "顾虑：上线没有安排维护窗口。",
                "同意：周日凌晨 2 点的窗口运维可接受。",
            ],
        }
        agree = "同意"
        verdict_ok = "达成共识：按分期方案上线。"
        verdict_cap = "到达轮次上限仍未完全收敛，按分期方案上线。"
        final_note = "板上先后留下了 {n} 条意见。"

    experts = ("finance", "legal", "ops")
    wf = Workflow()
    wf.channel("board", append())  # opinions accumulate across rounds
    wf.channel("round", last(0))

    def expert_node(name):
        agent = Agent(
            name,
            model=_model(*scripts[name]),
            instruction=(
                f"You are the {name} reviewer; state your position on the proposal."
                if lang == "en"
                else f"你是{name}评审，对方案给出你的立场。"
            ),
            bus=wf.bus,
        )

        async def run(_, ctx):
            # The expert thinks on its own; only its verdict lands on the board.
            # Earlier opinions travel with the task, so a real model can
            # actually respond to them (the scripted one plays the arc).
            digest = " | ".join(f"{op['by']}: {op['view']}" for op in ctx.shared["board"])
            task = f"round {ctx.shared['round'] + 1}: review the proposal"
            if digest:
                task += f". Board so far: {digest}"
            view = await agent.delegate(task)
            return {"board": [{"by": name, "round": ctx.shared["round"], "view": view}]}

        return run

    async def fanout(_, ctx):
        # Re-arm the experts and the moderator; the edges still decide that
        # experts run in parallel first and the moderator waits for them all.
        return Outcome(control=[Goto.rejoin(n) for n in (*experts, "moderate")])

    async def moderate(_, ctx):
        board, rnd = ctx.shared["board"], ctx.shared["round"]
        this_round = [op for op in board if op["round"] == rnd]
        converged = bool(this_round) and all(op["view"].startswith(agree) for op in this_round)
        # Round cap: the debate ends in a verdict even if consensus never forms
        # (e.g. a real model never says the scripted word for "agree").
        if converged or rnd + 1 >= 3:
            return go("final", verdict_ok if converged else verdict_cap)
        return go("fanout", round=rnd + 1)  # another round, objections stay on the board

    async def final(text, ctx):
        return f"{text} ({final_note.format(n=len(ctx.shared['board']))})"

    for name in experts:
        wf.add(name, expert_node(name))
        wf.edge("fanout", name)
        wf.edge(name, "moderate")
    wf.add("fanout", fanout)
    wf.add("moderate", moderate, join="all")  # every expert of this round, every round
    wf.add("final", final, terminal=True)
    wf.entry("fanout")
    return wf


# ---------------------------------------------------------------- memory (playground-only bonus): cross-session recall
async def _memory_regular(lang: str = "en"):
    mem = InMemoryMemory()
    # Long-term memory sedimented from earlier chats — it exists independently
    # of this conversation's context.
    if lang == "en":
        await mem.remember(
            "user's tea order preference: no sugar, less ice, keep answers short",
            tags=["preference"],
        )
        script = [
            "Sure — no sugar, less ice per your saved preference; order placed.",
            "I remember — no sugar, less ice again; placing the order now.",
        ]
        instruction = "You are an ordering assistant; honor the preferences in long-term memory."
    else:
        await mem.remember("用户点奶茶的偏好：默认无糖、去冰，回答尽量简短", tags=["偏好"])
        script = [
            "好的，按你记忆中的偏好做了无糖去冰，已下单。",
            "记得呢——还是无糖去冰，这就再帮你下一单。",
        ]
        instruction = "你是点单助手，要结合长期记忆里的偏好。"
    # Before think, the current question retrieves memories and splices them
    # into the system prompt; the model answers as if it remembers you.
    return Agent(
        name="regular",
        model=_model(*script),
        instruction=instruction,
        memory=mem,
    )


# The scenario table: key -> titles/blurbs/defaults in both languages, plus a
# builder (may be async) that takes the UI language.
SCENARIOS = [
    {
        "key": "01",
        "title": "01 Greet & order",
        "desc": "The smallest agent: think once, call one tool, answer.",
        "default": "Do you have taro bubble tea?",
        "title_zh": "01 问候点单",
        "desc_zh": "最小的 Agent：想一步、调一次工具、再回答。",
        "default_zh": "你们这儿有芋泥啵啵吗？",
        "build": _greeter,
        "is_async": False,
    },
    {
        "key": "02",
        "title": "02 Haggle · order approval",
        "desc": "After multi-round haggling, suspend before spending money and wait for your approval.",
        "default": "Buy me a bubble tea, as cheap as you can",
        "title_zh": "02 代购砍价·下单审批",
        "desc_zh": "多轮砍价后，动钱前在网页上挂起等你批准。",
        "default_zh": "帮我买杯奶茶，尽量便宜",
        "build": _trader,
        "is_async": False,
    },
    {
        "key": "03",
        "title": "03 Deep research · context compaction",
        "desc": "Multiple search rounds; early results past capacity are compacted into summaries.",
        "default": "Research the new-energy sector for me",
        "title_zh": "03 深度研究·上下文压缩",
        "desc_zh": "连查多轮，超预算的早期检索被压成摘要。",
        "default_zh": "帮我研究新能源赛道",
        "build": _research,
        "is_async": False,
    },
    {
        "key": "04",
        "title": "04 Compliance audit · parallel + approval",
        "desc": "Two parallel checks converge; freezing suspends for approval.",
        "default": "Audit account A1",
        "title_zh": "04 合规审计·并行+审批",
        "desc_zh": "两路并行核查、汇合，冻结前挂起审批。",
        "default_zh": "审计账户 A1",
        "build": _compliance,
        "is_async": False,
    },
    {
        "key": "05",
        "title": "05 Code detective · MCP + skills",
        "desc": "MCP tools normalized at the boundary; a skill guides fail-fix-rerun until green.",
        "default": "test_x keeps failing; fix it for me",
        "title_zh": "05 代码侦探·MCP+技能",
        "desc_zh": "MCP 工具在边界拉平，按技能指引失败再改到转绿。",
        "default_zh": "test_x 一直红，帮我修好",
        "build": _detective,
        "is_async": True,
    },
    {
        "key": "06",
        "title": "06 After-sales refund · supervisor & specialists",
        "desc": 'The supervisor\'s "tools" are other agents: dispatch billing/risk and answers come back; risk delegates again — three levels.',
        "default": "Order O-1234: customer charged but nothing shipped. Refund?",
        "title_zh": "06 售后退款·主管与专家",
        "desc_zh": "主管的“工具”是别的 Agent：派账单/风控专家拿回结论；风控再往下委派，长出三层。",
        "default_zh": "订单 O-1234：扣了款一直没发货，该不该退款？",
        "build": _after_sales,
        "is_async": False,
    },
    {
        "key": "07",
        "title": "07 Incident response · delegate & handoff",
        "desc": "Parallel diagnosis via delegation (call), then `go` hands off to the repairer (transfer).",
        "default": "Order-service latency is spiking",
        "title_zh": "07 故障应急·委派与接力",
        "desc_zh": "并行委派诊断（call），再 go 到修复 Agent 接力（transfer）。",
        "default_zh": "订单服务延迟飙升",
        "build": _aiops,
        "is_async": False,
    },
    {
        "key": "08",
        "title": "08 Write-review-revise · multi-agent",
        "desc": "Writer/critic/reviser agents; a conditional branch picks revise or finalize, then converge.",
        "default": "Write a quarterly business summary",
        "title_zh": "08 撰稿-审阅-修订·多 Agent",
        "desc_zh": "撰稿/审阅/修订三个 Agent，按审阅结果走条件分支再汇合定稿。",
        "default_zh": "写一份季度经营结论",
        "build": _review_team,
        "is_async": False,
    },
    {
        "key": "09",
        "title": "09 Service audit · orchestrator-worker",
        "desc": "How many reviewers? The planner reads the live catalog; Send stamps one template copy per service, all in one wave.",
        "default": "Audit every deployed service before the release",
        "title_zh": "09 服务巡检·编排者-工人",
        "desc_zh": "几个审查员？规划者读线上目录，Send 按服务压出模板拷贝，同一波并发。",
        "default_zh": "发布前把已部署的服务都巡检一遍",
        "build": _orchestrator,
        "is_async": False,
    },
    {
        "key": "10",
        "title": "10 Proposal review · blackboard",
        "desc": "Experts only write to a shared board; the moderator reads it — not converged, re-arm everyone for another round.",
        "default": "Should we roll out the new pricing engine next week?",
        "title_zh": "10 方案评审·黑板",
        "desc_zh": "专家只往共享黑板写意见，裁判读板裁决；未收敛就重开一轮。",
        "default_zh": "新定价引擎下周上线，行吗？",
        "build": _blackboard,
        "is_async": False,
    },
    {
        # Playground-only bonus (no numbered counterpart in examples/): the
        # numbered scenarios 01-10 mirror the business examples exactly; this
        # one needs the page's chat continuation to feel real.
        "key": "memory",
        "title": "＋ Long-term memory · cross-session recall",
        "desc": "Memory outlives the chat; retrieved and injected before think — the model remembers you.",
        "default": "Order me a bubble tea",
        "title_zh": "＋ 长期记忆·跨会话召回",
        "desc_zh": "记忆独立于本次对话存在，think 前检索并注入，模型像记得你。",
        "default_zh": "帮我点杯奶茶",
        "build": _memory_regular,
        "is_async": True,
    },
]


def get_scenario(key: str):
    for s in SCENARIOS:
        if s["key"] == key:
            return s
    return None
