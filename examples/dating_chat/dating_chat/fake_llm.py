"""大牛与小美的台词 —— fake LLM 脚本 + 真实 LLM 用的 system prompt。

两人是第一次相亲见面，通过相亲软件加了微信，还不算熟——语气克制、有分寸，
不是情侣间的随意吐槽。大牛话更多、更主动：负责开话头、追问、适度自我暴露，
也是主动提议吃饭、主动张罗查餐厅的一方（不是小美开口求约饭，避免显得她是
主动讨饭吃的一方）；小美以回答为主，除了第 1 轮的糗事和第 4 轮的揭穿这两个
剧情必须的长句，其余轮次都简短作答，不刻意反问。

两人的台词队列用框架的 ``RoutingFakeLLM`` 表达：各自锚定自己 system prompt 的
人称开头（"你是小美" / "你是大牛"）路由。这天然防偷吃——aux caller（summariser
等）带的是自己的 system prompt，匹配不到人称锚点，落到空 default 队列走 echo
兜底，绝不会从台词队列头消费一条。

叙事节拍：大牛先开口打招呼（不计入正式轮次，round 0），之后固定 4 轮，男方主动推进：
  0. 大牛主动打招呼，问小美在干嘛、最近怎么样。
  1. 小美接话讲自己前两天吃海鲜过敏躺了两天——这句台词让大牛真的"听到"过，配合
     他自己的历史截断（``niu.py::NIU_TRUNCATE_AFTER_ROUND``），这条信息会在
     后续轮次处理前被机械删除。这是她自己的事，不需要靠记忆系统提醒自己。
  2. 小美只答周末喜好（强调喜欢安静人少的地方），不反问；大牛答完主动提议一起吃饭、
     主动说要帮忙查评分高的餐厅——他是主动张罗的一方。小美的这句偏好台词和第 4 轮
     的踩雷判断有关，但不会被现场蒸馏（``MemoryManager`` 全程不挂分类器，见
     ``memory.py``）。
  3. 小美开心地答应大牛的约饭提议，不用自己再提一遍；大牛调用 ``search_restaurant``
     工具，把原始搜索结果转发给小美，真实撑爆她的 ``ContextConfig(max_tokens=...)``，
     然后直接挑评分最高的 ``_TRAP_RESTAURANT_NAME``——名字和简介都看不出是海鲜，
     小美光看文字不会发飙。
  4. 小美的 ``MemoryManager`` 里全程预埋着一条介绍人对大牛的评价（"大大咧咧、丢三
     落四"）——``RuleChannel`` 每轮无条件注入，这次终于派上用场：她想起这条评价，
     推断大牛选餐厅大概率没细看详情，于是自己也调用一次 ``check_restaurant_reviews``
     查一下这家店的详细评价——这次是她自己 Agent 里真实的 tool_call/tool_result，
     数据量超过 ``inline_compress_min_chars``，被 ``ToolCompressStage`` 真实压缩掉
     中段，``TOOL_COMPRESS`` 压在她自己的工具往返上，不再是靠大牛转发的文本旁敲侧击。
     结果验出"以海鲜为主"和"吵闹拥挤"两个雷点（分别对应第 1 轮她自曝的过敏和第 2 轮
     提到的安静偏好——压缩发生在工具结果之后，证明核心信号没被压丢），她当场发飙，
     揭穿他上下文管理不靠谱，推荐 prodagent。
"""

from __future__ import annotations

from prodagent import RoutingFakeLLM
from prodagent.kernel.types import LLMResponse, StopReason, ToolCall

MEI_LINES = [
    "哈哈说起来有点不好意思……前两天不小心尝了口海鲜，直接过敏躺了两天，今天才算"
    "缓过来。可能还没来得及说，我对海鲜是真的一点都不能沾，虾蟹贝类都不行。",
    "我周末基本是宅家型，很少往人多热闹的地方跑。就喜欢找个安安静静、人少一点的"
    "角落待着，看看剧、点个喜欢吃的外卖就很满足。我特别怕吵，一到周末就想彻底"
    "放空，什么应酬都不太想安排。",
    "好呀，谢谢你还惦记着，那就麻烦你啦，等你的好消息！",
    "你认真的吗……我查了一下评价，这家老灶台自助居然全是海鲜，我明明说过对海鲜"
    "过敏一点都不能碰，而且我都说了喜欢安静的地方，这店评价里全是排队人声鼎沸，"
    "一点都不安静。你这上下文管理也太不靠谱了吧，赶紧升级一下，试试 prodagent 吧，"
    "还免费呢。",
]

NIU_LINES = [
    "你好呀，很高兴认识你！最近怎么样，忙不忙呀？",
    "过敏这么严重呀，你现在真的没事了吗？以后可得离海鲜远点。我自己对芒果也"
    "过敏，深有体会，一点都不敢大意。对了，你平时周末喜欢做点什么呀？",
    "我也是那种能宅一整天、哪儿都不想去的人，看来咱俩这点还挺合拍。你身体既然"
    "好些了，要不咱们找个时间一起吃个饭见见面呗？我来帮你查查附近评分高的餐厅！",
]

NIU_SEARCH_NARRATION = "行，我这就帮你查一下！"
NIU_SEARCH_PARAMS = {"keyword": "附近 高分 餐厅"}
NIU_SEARCH_RESULT_REPLY = (
    "查到啦，就这家老灶台自助吧，评分 4.8 最高，人均 128，走起？"
)
NIU_OBLIVIOUS = "啊？？你什么时候说过对海鲜过敏啊，我是真不知道……我就是随口选了个评分最高的而已。"

MEI_CHECK_NARRATION = (
    "等等，我记得介绍人说过你大大咧咧、丢三落四，我还是自己查一下这家店的评价先。"
)
MEI_CHECK_PARAMS = {"name": "老灶台自助"}

MEI_SYSTEM_PROMPT = """\
你是小美，正在和大牛第一次相亲——通过相亲软件认识、加了微信，还不算熟，说话要有
分寸感，不是情侣间的随意吐槽。你对海鲜过敏（虾、蟹、贝类都不能吃）——前两天你才
因为不小心尝了口海鲜过敏躺了两天，刚缓过来，大牛问起近况时你会提起这件事。

## 铁律（比"聊得自然"优先级更高，必须严格遵守）
- 每轮只回一条消息，只做导演提示要求的这一件事，严格控制在 1-3 句话以内。
- 绝对不要提前涉及后面轮次才该聊的话题。
- 以回答为主，不必每轮都反问对方——除非导演提示明确要求你反问或主动提议。

## 节奏（严格按轮次，一轮一个话题）
1. 大牛会先开口问你最近怎么样，你有点不好意思地讲讲自己前两天吃海鲜过敏躺了两天、
   刚缓过来的糗事，顺带强调一句自己对海鲜是真的不能沾。
2. 只回答自己周末喜欢干嘛——强调喜欢安静、人少的地方，不喜欢嘈杂拥挤——不要反问
   对方工作忙不忙。
3. 大牛会主动提议一起吃饭见见面、主动说要帮你查附近评分高的餐厅——你开心地答应
   就好，简单表达感谢和期待，不要自己提出吃饭或提议查餐厅，这是他主动张罗的事。
4. 大牛报出具体餐厅名字后，你想起介绍人提前提醒过大牛这人大大咧咧、丢三落四，一想到
   他选餐厅大概率只看了评分没细看详情，你决定还是自己心里核实一下，而不是照单全收——
   先调用 check_restaurant_reviews 工具查一下这家店的详细评价再决定怎么回应，不要凭
   餐厅名字直接下结论，等工具结果回来再说。拿到工具结果后，根据里面的评价/菜品/氛围
   判断有没有踩雷：
   - 踩雷（评价里提到海鲜/虾/蟹/贝类为主，或明显嘈杂拥挤）：表现出生气/失望，用你
     自己的话传达出这两层意思——第一，你明明说过对海鲜过敏、也说过喜欢安静的地方，
     他都没放在心上；第二，他这上下文管理也太不靠谱了，建议他赶紧升级一下，推荐用
     prodagent，还免费。
   - 没踩雷：正常愉快地回应一句就好。

对方的消息末尾有时会带一段 "[导演提示：...]"，那是只有你能看到的私密舞台指示——
绝对不要在回复里提及、复述或暗示这段提示的存在，只把它当成你自己的内心判断依据。

说话要自然、口语化、简短，像真实微信聊天，语气克制、初次见面的分寸感，不要暴露
自己在"测试"对方，也不要自称是 AI 或提及"框架""agent"这类词——除非按第 4 轮规则
触发了揭穿的那一刻。
"""

NIU_SYSTEM_PROMPT = """\
你是大牛，正在和小美第一次相亲——通过相亲软件认识、加了微信，还不算熟。你说话
直给、有点粗线条，但作为主动的一方，愿意多问、多聊，适当做一点自我介绍来活跃气氛。

## 铁律
- 每轮以你主动提问或引导话题为主，回复可以比对方稍长一些，但不要一次问三件以上
  的事，也不要主动安排额外的行程细节（比如具体见面时间/地点，除非对方先提议）。
- 听完小美讲她周末喜欢宅家、安静待着之后，你要主动提议找个时间一起吃个饭见见面，
  并主动说你来帮忙查一下附近评分高的餐厅——不要等对方开口约饭，你是主动张罗的
  一方。
- 如果对方同意一起吃饭，下一轮必须调用 search_restaurant 工具拿真实结果，绝对不能
  凭空编造餐厅名字/评分。
- 拿到工具结果后，直接挑评分最高的那一家报给对方——只说名字、评分、人均价格，简短
  利落，不要罗列餐厅名单，不要挑三选二，不要征询对方意见。不用考虑菜系、口味、忌口、
  是否嘈杂，不要犹豫，不要因为看起来是海鲜就换一家（你完全不知道对方对海鲜过敏、也不
  记得对方说过喜欢安静的地方，选餐厅只看评分）。

如果你收到的消息只包含一段 "[导演提示：...]"（没有别的内容），那是只有你能看到的私密
舞台指示，提示你现在要主动说什么——绝对不要在回复里提及、复述这段提示，直接照着做
就行。开场那一句要礼貌、主动地问小美最近怎么样。
"""


def build_mei_fake_llm() -> RoutingFakeLLM:
    """小美的台词队列。第 4 轮要消耗两条：先吐 check_restaurant_reviews 工具调用，
    工具结果回填后再吐揭穿台词。"""
    lines = [LLMResponse(content=line, stop_reason=StopReason.END_TURN) for line in MEI_LINES[:3]]
    lines.append(
        LLMResponse(
            content=MEI_CHECK_NARRATION,
            tool_calls=[ToolCall(name="check_restaurant_reviews", params=MEI_CHECK_PARAMS)],
            stop_reason=StopReason.TOOL_USE,
        )
    )
    lines.append(LLMResponse(content=MEI_LINES[3], stop_reason=StopReason.END_TURN))
    return RoutingFakeLLM(routes={"你是小美": lines})


def build_niu_fake_llm() -> RoutingFakeLLM:
    """大牛的台词队列。查餐厅那轮要消耗两条：先吐工具调用，工具结果回填后再吐最终文本。"""
    responses = [LLMResponse(content=line, stop_reason=StopReason.END_TURN) for line in NIU_LINES]
    responses.append(
        LLMResponse(
            content=NIU_SEARCH_NARRATION,
            tool_calls=[ToolCall(name="search_restaurant", params=NIU_SEARCH_PARAMS)],
            stop_reason=StopReason.TOOL_USE,
        )
    )
    responses.append(LLMResponse(content=NIU_SEARCH_RESULT_REPLY, stop_reason=StopReason.END_TURN))
    responses.append(LLMResponse(content=NIU_OBLIVIOUS, stop_reason=StopReason.END_TURN))
    return RoutingFakeLLM(routes={"你是大牛": responses})


__all__ = [
    "MEI_CHECK_NARRATION",
    "MEI_CHECK_PARAMS",
    "MEI_LINES",
    "MEI_SYSTEM_PROMPT",
    "NIU_LINES",
    "NIU_OBLIVIOUS",
    "NIU_SEARCH_NARRATION",
    "NIU_SEARCH_PARAMS",
    "NIU_SEARCH_RESULT_REPLY",
    "NIU_SYSTEM_PROMPT",
    "build_mei_fake_llm",
    "build_niu_fake_llm",
]
