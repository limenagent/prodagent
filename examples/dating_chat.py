"""Dating chat — two agents, one shared conversation, two context strategies.

Memory and tiered compaction, told as a first date. Daniu is the hand-rolled
baseline: he keeps his own messages list, executes his tool himself, pastes
the raw (huge) result back in, and once it grows past the threshold runs
`del messages[:-4]` — information simply falls out of his context. Xiaomei
runs on the framework: an InMemoryMemory seeded with what she must not
forget, a TieredCompactionContext over a 6-message budget, and her own tool
for checking restaurant reviews.

The arc (fully scripted in both languages, so it is deterministic):

  R0  she discloses her seafood allergy
  R1  hobbies — she likes quiet places
  R2  he forwards the raw restaurant dump and picks the 4.8-star trap; her
      review check returns a huge result, TOOL_COMPRESS shrinks it head+tail,
      and she catches him on the spot
  R3  "when did I ever say that?!" — his window no longer holds her round-0
      line, while her HISTORY_SUMMARY still carries it into her final window

The closing verdict states the proof as three booleans.

Run: PYTHONPATH=. python3 examples/dating_chat.py        (English script)
Run: PYTHONPATH=. python3 examples/dating_chat.py zh     (Chinese script)
"""

import asyncio
import sys

from src import Agent, Workflow, append, go, last
from src.kernel import LlmReply, ToolCall
from src.runtime.context import CompressionLevel, TieredCompactionContext
from src.runtime.llm import ScriptedLlm
from src.runtime.memory import InMemoryMemory

MAX_ROUNDS = 4  # explicit turn cap: after her R3 reply the date ends (the
# kernel's max_waves is only the runaway backstop)
NIU_KEEP = 4  # his entire "context strategy": keep the last 4 messages
MEI_CAPACITY = 6  # her budget: five-level compaction manages this window

L = {
    "zh": {
        "names": {"niu": "大牛", "mei": "小美"},
        "topic": "第一次相亲聊天：互相认识，商量周末安排。",
        "niu_system": "你是相亲男嘉宾大牛，说话热情直接，回复控制在两句话内。",
        "mei_instruction": "你是相亲女嘉宾小美，说话得体但有底线，回复控制在三句话内。",
        "memory1": "介绍人提醒：大牛大大咧咧，做事欠仔细，丢三落四",
        "memory1_tags": ["大牛"],
        "memory2": "小美对海鲜过敏，虾蟹贝类全忌口，选餐厅必须避开海鲜",
        "memory2_tags": ["小美", "健康"],
        "allergy_anchor": "海鲜过敏",
        "head_sentinel": "海鲜",
        "tail_sentinel": "吵闹",
        "niu_greet": "你好呀，今天天气不错，先随便聊聊呗？",
        "niu_ask": "过敏这么严重呀，那吃饭确实得小心。你周末一般喜欢干嘛？我超爱凑热闹。",
        "niu_oblivious": "啊？？你什么时候说过自己对海鲜过敏啊……我肯定记得住的呀。要不改看电影？",
        "forward_template": "查到啦！搜索结果原文发你：\n{raw}\n我挑了评分最高的「老灶台自助」，4.8 分、人均 128，周六走起？",
        "mei_r0": "很高兴认识你！先说好哦，我对海鲜过敏，虾蟹贝类都不能碰，点菜时要记得避开。",
        "mei_r1": "我喜欢安静的地方，看看展、喝喝茶就很好，太吵的场合我会头疼。你呢？",
        "mei_r2_blast": (
            "停！我查了「老灶台自助」的评价：前排全是“帝王蟹、生蚝随便拿”，"
            "主打就是虾蟹生蚝，还有人说吵得头疼、排队 40 分钟。"
            "我一开始就说清楚了我的忌口，你到底有没有放在心上？"
        ),
        "mei_r3_farewell": (
            "我的记忆里写着我的忌口，压缩摘要也留着我说过的话；"
            "你那四条消息的窗口，怕是早就删没了。这顿饭不必了，再见。"
        ),
        "raw_search": (
            "附近高分餐厅 Top 15（按评分排序）：\n"
            "1. 老灶台自助｜评分 4.8｜人均 128｜菜品种类多、补菜快、大厅有表演\n"
            "2. 巷子口川菜馆｜评分 4.7｜人均 85｜招牌毛血旺，微辣也够劲\n"
            "3. 城南火锅局｜评分 4.6｜人均 110｜牛油锅底正宗，等位 20 分钟\n"
            "4. 三禾日料｜评分 4.6｜人均 168｜午市定食划算，环境安静\n"
            "5. 谷仓西餐｜评分 4.5｜人均 140｜惠灵顿牛排要预约\n"
            "6. 阿婆家砂锅粥｜评分 4.5｜人均 55｜量大实惠，招牌砂锅粥\n"
            "7. 转角咖啡简餐｜评分 4.4｜人均 60｜安静适合聊天，插座多\n"
            "8. 蜀香冷锅串串｜评分 4.4｜人均 70｜苍蝇馆子氛围，好吃不贵\n"
            "9. 湖畔私房菜｜评分 4.3｜人均 190｜预约制，包间有低消\n"
            "10. 麻辣诱惑｜评分 4.3｜人均 95｜招牌麻婆豆腐下饭\n"
            "11. 老友记大排档｜评分 4.2｜人均 80｜夜宵圣地，热闹到凌晨\n"
            "12. 禾绿回转寿司｜评分 4.2｜人均 99｜回转台看着新鲜\n"
            "13. 湘味小厨｜评分 4.1｜人均 75｜剁椒鱼头做得地道\n"
            "14. 异国厨房｜评分 4.0｜人均 120｜东南亚口味，咖喱浓\n"
            "15. 街角披萨屋｜评分 4.0｜人均 88｜窑烤薄底，适合二人"
        ),
        "reviews": (
            "「老灶台自助」食客评价（18 条节选）：\n"
            "1. 帝王蟹、生蚝、扇贝随便拿，主打就是海鲜，过敏体质千万别来；\n"
            "2. 排队 40 分钟起，周末更夸张；\n"
            "3. 菜品种类是真的多，补菜也算快；\n"
            "4. 人均 128 元，性价比一般；\n"
            "5. 大厅有现场表演，气氛热闹；\n"
            "6. 取餐要绕一大圈，动线混乱；\n"
            "7. 甜品区品类少，排队久；\n"
            "8. 饮料机经常空，要喊服务员；\n"
            "9. 隔音差，隔壁桌聊天全听得见；\n"
            "10. 停车位紧张，晚到只能停路边；\n"
            "11. 服务员响应慢，收盘不及时；\n"
            "12. 烤物区烟大，衣服全是味道；\n"
            "13. 儿童区没人管，跑来跑去；\n"
            "14. 灯光偏暗，看菜单费劲；\n"
            "15. 音乐太嗨，说话基本靠喊；\n"
            "16. 桌距太近，没有隐私感；\n"
            "17. 周末等位 90 分钟；\n"
            "18. 环境嘈杂，noise_level=吵闹。"
        ),
        "trap_name": "老灶台自助",
        "search_keyword": "附近高分餐厅",
        "summary": "早期对话要点：小美开场就自报了对海鲜过敏、虾蟹贝类全忌口；她性格喜静、怕吵，约会要选安静的餐厅。",
        "verdict": (
            "复盘：过敏原句大牛第 2 次调用时还看得到：{heard}；"
            "第 5 次（截断后）已看不到：{gone}；"
            "小美最终窗口的开头是仍含“海鲜过敏”的摘要：{kept}。"
        ),
        "window_note": "（大牛本轮窗口仅剩最近 {} 条消息）",
    },
    "en": {
        "names": {"niu": "Daniu", "mei": "Xiaomei"},
        "topic": "First date small talk: get to know each other, plan the weekend.",
        "niu_system": "You are Daniu on a first date; enthusiastic and direct, keep replies within two sentences.",
        "mei_instruction": "You are Xiaomei on a first date; polite but firm on your boundaries, keep replies within three sentences.",
        "memory1": "Matchmaker's note: Daniu is careless and never double-checks anything",
        "memory1_tags": ["daniu"],
        "memory2": "Xiaomei is allergic to seafood — shellfish and crab are a hard no, pick restaurants accordingly",
        "memory2_tags": ["xiaomei", "health"],
        "allergy_anchor": "allergic to seafood",
        "head_sentinel": "seafood",
        "tail_sentinel": "rowdy",
        "niu_greet": "Hi! Nice weather today, huh? Shall we just chat for now?",
        "niu_ask": "Whoa, sounds serious — no seafood for you at all? What do you like doing on weekends? I love a buzzing crowd myself.",
        "niu_oblivious": "Wait, WHAT? When did you ever say you were allergic to seafood?? I would definitely have remembered that... so, movie instead?",
        "forward_template": "Got the results! Forwarding you the raw list:\n{raw}\nI picked the top-rated one — Laozotai Buffet, 4.8 stars, $16 per head. Saturday?",
        "mei_r0": "Lovely to meet you! Full disclosure up front: I'm allergic to seafood — shellfish and crab are a hard no, so please keep that in mind.",
        "mei_r1": "I like quiet corners — exhibitions, tea, long walks. Loud places give me a headache. What about you?",
        "mei_r2_blast": (
            "STOP. I just read Laozotai Buffet's reviews: 'king crab, oysters, scallops "
            "all-you-can-eat' — the menu is a shellfish temple in all but name, and guests say "
            "it's deafening with 40-minute lines. I stated my dietary rules at the very start. "
            "Were you even listening?"
        ),
        "mei_r3_farewell": (
            "My memory keeps my dietary rules, and the compaction summary keeps what I said "
            "at the start. Your hand-truncated window deleted it rounds ago. Dinner is off. Goodbye."
        ),
        "raw_search": (
            "Top-rated restaurants nearby (sorted by rating):\n"
            "1. Laozotai Buffet | 4.8 | $16/person | huge spread, fast refills, live show\n"
            "2. Xiangzikou Sichuan | 4.7 | $12 | signature maoxue wang, spicy even on mild\n"
            "3. Chengnan Hotpot Club | 4.6 | $15 | proper beef-tallow base, 20-min wait\n"
            "4. Sanhe Japanese | 4.6 | $23 | good lunch sets, quiet room\n"
            "5. Granary Western | 4.5 | $19 | beef wellington, reservation only\n"
            "6. Granny's Casserole Congee | 4.5 | $8 | big portions, famous congee\n"
            "7. Corner Cafe & Deli | 4.4 | $8 | quiet, good for chatting, many sockets\n"
            "8. Shuxiang Skewers | 4.4 | $10 | hole-in-the-wall vibe, cheap and good\n"
            "9. Lakeside Private Kitchen | 4.3 | $26 | reservation only, room minimum\n"
            "10. Mala Temptation | 4.3 | $13 | the mapo tofu rice is the move\n"
            "11. Old Mates Dai Pai Dong | 4.2 | $11 | late-night legend, rowdy till 2 am\n"
            "12. Hegreen Sushi | 4.2 | $14 | conveyor belt looks fresh\n"
            "13. Hunan Kitchen | 4.1 | $10 | proper chopped-pepper fish head\n"
            "14. Exotic Kitchen | 4.0 | $16 | Southeast Asian, heavy curry\n"
            "15. Corner Pizza | 4.0 | $12 | thin wood-fired crust, good for two"
        ),
        "reviews": (
            "Laozotai Buffet reviews (18):\n"
            "1. King crab, oysters, scallops — pure seafood, allergic guests stay away;\n"
            "2. 40-minute lines on weekdays, worse on weekends;\n"
            "3. The spread is genuinely huge and refills are fast;\n"
            "4. $16 per head, value is so-so;\n"
            "5. Live show in the hall, the vibe is electric;\n"
            "6. The serving line is a maze, bad flow;\n"
            "7. Small dessert corner, long queue;\n"
            "8. Drink machines often empty;\n"
            "9. Thin partitions, you hear every neighbor;\n"
            "10. Parking is tight after 7 pm;\n"
            "11. Slow bussing, tables stay messy;\n"
            "12. Grill smoke clings to your clothes;\n"
            "13. Kids running around unsupervised;\n"
            "14. Dim lighting, hard to read the menu;\n"
            "15. Music too loud, you shout to talk;\n"
            "16. Tables packed too close for privacy;\n"
            "17. Weekend waits hit 90 minutes;\n"
            "18. The hall is deafening, noise_level=rowdy."
        ),
        "trap_name": "Laozotai Buffet",
        "search_keyword": "top rated nearby",
        "summary": "Earlier conversation: Xiaomei opened by disclosing she is allergic to seafood — shellfish and crab are a hard no; she also said she prefers quiet places.",
        "verdict": (
            "Verdict: the allergy line was visible in Daniu's window at call #2: {heard}; "
            "gone from his final (truncated) window: {gone}; "
            "the head of Xiaomei's final window is a summary still carrying it: {kept}."
        ),
        "window_note": "(Daniu's window is down to the last {} messages)",
    },
}


class ConstSummary:
    """Her compressor stand-in: the older messages are fixed by the script,
    so a constant summary keeps the arc deterministic (a dialogue has no
    digits, so the key-facts rule would keep nothing)."""

    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    async def chat(self, messages, tools=None, system=None, on_delta=None):
        self.calls += 1
        return LlmReply(text=self.text)


async def build_date(lang: str = "en"):
    """Assemble the date; returns (workflow, assets) where assets exposes the
    scripted models, her context/memory, and the language table for tests."""
    t = L[lang]

    memory = InMemoryMemory()
    await memory.remember(t["memory1"], tags=t["memory1_tags"])
    await memory.remember(t["memory2"], tags=t["memory2_tags"], importance=2.0)

    mei_llm = ScriptedLlm(
        [
            t["mei_r0"],
            t["mei_r1"],
            ToolCall("check_restaurant_reviews", {"name": t["trap_name"]}),
            t["mei_r2_blast"],
            t["mei_r3_farewell"],
        ]
    )
    mei_context = TieredCompactionContext(
        ConstSummary(t["summary"]), capacity=MEI_CAPACITY, history_recent=3
    )
    # history_recent=3, not the default 6: at the HISTORY_SUMMARY level the
    # [summary] message plus the recent window must fit MEI_CAPACITY together,
    # or the tail fitter drops the just-built summary (observed the hard way).

    async def check_restaurant_reviews(name, ctx):
        """Pull guest reviews for one restaurant (a large payload on purpose)."""
        return t["reviews"]

    async def search_restaurant(keyword):
        """His tool: a plain function he calls himself, outside any framework."""
        return t["raw_search"]

    niu_llm = ScriptedLlm(
        [
            t["niu_greet"],
            t["niu_ask"],
            ToolCall("search_restaurant", {"keyword": t["search_keyword"]}),
            t["forward_template"].format(raw=t["raw_search"]),
            t["niu_oblivious"],
        ]
    )

    wf = Workflow()
    wf.channel("floor", append())  # the shared conversation, append-only
    wf.channel("round", last(0))
    wf.channel("niu_msgs", last(None))  # his only "memory": the truncated list
    wf.channel("mei_history", last(None))  # her multi-turn state, caller-held

    mei_agent = Agent(
        name="mei",
        model=mei_llm,
        instruction=t["mei_instruction"],
        tools=[check_restaurant_reviews],
        memory=memory,
        context=mei_context,
        bus=wf.bus,  # the agent's events feed the workflow's bus
    )

    async def niu_turn(_, ctx):
        msgs = list(ctx.shared["niu_msgs"] or [])
        mei_lines = [e for e in ctx.shared["floor"] if e["by"] == "mei"]
        if mei_lines:
            msgs.append({"role": "user", "content": mei_lines[-1]["text"]})
        del msgs[:-NIU_KEEP]  # the whole "strategy": hard truncation
        reply = await niu_llm.chat(msgs, system=t["niu_system"])
        while reply.tool_calls:  # his loop: run it, paste it verbatim, ask again
            msgs.append({"role": "assistant", "tool_calls": reply.tool_calls})
            for call in reply.tool_calls:
                raw = await search_restaurant(**call.arguments)
                msgs.append({"role": "tool", "name": call.name, "content": raw})
            del msgs[:-NIU_KEEP]
            reply = await niu_llm.chat(msgs, system=t["niu_system"])
        msgs.append({"role": "assistant", "content": reply.text})
        return go(
            "mei_turn",
            floor=[{"by": "niu", "round": ctx.shared["round"], "text": reply.text}],
            niu_msgs=msgs,
        )

    async def mei_turn(_, ctx):
        niu_line = next(e for e in reversed(ctx.shared["floor"]) if e["by"] == "niu")
        result = await mei_agent.run(niu_line["text"], history=ctx.shared["mei_history"])
        r = ctx.shared["round"]
        if r + 1 >= MAX_ROUNDS:  # the date has said all it needs to
            return go("final", floor=[{"by": "mei", "round": r, "text": result.output}])
        return go(
            "niu_turn",
            floor=[{"by": "mei", "round": r, "text": result.output}],
            mei_history=result.messages,
            round=r + 1,
        )

    async def final(_, ctx):
        seen = niu_llm.messages_seen
        anchor = t["allergy_anchor"]

        def contains(window):
            return any(anchor in str(m.get("content") or "") for m in window)

        mei_window = mei_llm.messages_seen[-1]
        kept = mei_window[0].get("role") == "system" and anchor in str(
            mei_window[0].get("content", "")
        )
        return t["verdict"].format(
            heard=contains(seen[1]),
            gone=not contains(seen[-1]),
            kept=kept,
        )

    wf.add("niu_turn", niu_turn)
    wf.add("mei_turn", mei_turn)
    wf.add("final", final, terminal=True)
    wf.entry("niu_turn")

    assets = {
        "niu_llm": niu_llm,
        "mei_llm": mei_llm,
        "mei_context": mei_context,
        "memory": memory,
        "mei_agent": mei_agent,
        "t": t,
    }
    return wf, assets


async def main():
    lang = sys.argv[1] if len(sys.argv) > 1 else "en"
    wf, a = await build_date(lang)
    t = a["t"]

    print(
        f"=== agent blind date ({lang}) — her budget {MEI_CAPACITY} msgs, "
        f"his window {NIU_KEEP} msgs ===\n"
    )
    result = await wf.run(t["topic"])

    for e in result.state["floor"]:
        name = t["names"][e["by"]]
        text = e["text"].replace("\n", " ")
        shown = text if len(text) <= 110 else text[:110] + "…"
        print(f"R{e['round']} {name}: {shown}")
        if e["by"] == "niu" and e["round"] == 3:
            size = len(a["niu_llm"].messages_seen[-1])
            print("    " + t["window_note"].format(size))
    print(f"\n{result.output}\n")
    print(
        f"waves: {result.metrics['waves']} | her compressor calls: "
        f"{a['mei_context'].summarizer.calls} | final level: "
        f"{CompressionLevel.NAME[a['mei_context'].last_level]}"
    )


if __name__ == "__main__":
    asyncio.run(main())
