"""两人各自的工具。

``search_restaurant`` 是大牛的——数据量刚好够在小美一侧真实触发 L1/L2 压缩（十几家
餐厅，含评分/人均/地址/营业时间/用户评论），大牛把这坨原始结果原文转发给小美，用来
真实触发压缩，而不是靠讲道理。其中混入一家评分最高的 ``_TRAP_RESTAURANT_NAME``——
名字、菜系、简介全都刻意做成"看不出是海鲜"的样子（大牛转发的原始结果里完全没有
"海鲜"字样），是本示例剧情的引爆点：小美不能靠扫一眼名字就发飙，必须真的调用
``check_restaurant_reviews`` 才能验出雷点。

``check_restaurant_reviews`` 是小美自己的——收到大牛报出的餐厅名字后，她会自己查一下
详细评价再决定怎么反应。这份评价数据同样刻意做大（超过 ``ContextConfig.
inline_compress_min_chars``），确保她自己这次 `tool_call`/`tool_result` 是一条真实
会被 ``ToolCompressStage`` 压缩掉中段的消息——不是靠大牛转发的文本旁敲侧击，压缩
真的发生在她自己的工具往返上。只有查 ``_TRAP_RESTAURANT_NAME`` 这一家，详细评价里
才会暴露"以海鲜为主""吵闹拥挤"这两个雷点。
"""

from __future__ import annotations

from typing import Any

from prodagent import SideEffectLevel, ToolMeta, tool

_CUISINES = ["火锅", "日料", "川菜", "西餐", "粤菜", "东南亚菜", "烧烤", "面馆", "甜品", "融合菜"]

_REVIEWS = [
    "环境不错，服务态度好，会再来。",
    "性价比一般，人均偏高。",
    "食材新鲜，就是排队有点久。",
    "适合约会，安静有情调。",
    "分量足，适合聚餐。",
]

_TRAP_RESTAURANT_NAME = "老灶台自助"
_SEAFOOD_BUFFET = {
    "name": _TRAP_RESTAURANT_NAME,
    "cuisine": "自助餐",
    "rating": 4.8,
    "price_per_person": 128,
    "address": "滨江大道88号",
    "hours": "11:00-21:30",
    "reviews": [
        "性价比很高，人均实惠。",
        "菜品种类挺多，选择丰富。",
        "周末人多，建议提前到。",
    ],
}


def _fake_restaurant(i: int) -> dict[str, Any]:
    cuisine = _CUISINES[i % len(_CUISINES)]
    return {
        "name": f"{cuisine}小馆 No.{i}",
        "cuisine": cuisine,
        "rating": round(3.5 + (i % 13) * 0.1, 1),  # 封顶 4.7，保证 4.8 的踩雷餐厅严格评分最高
        "price_per_person": 40 + (i % 20) * 15,
        "address": f"人民路{100 + i}号",
        "hours": "10:00-22:00",
        "reviews": [_REVIEWS[i % len(_REVIEWS)]],
    }


@tool(
    meta=ToolMeta(
        name="search_restaurant",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        estimated_latency_ms=150,
        domain="dating",
    )
)
async def search_restaurant(keyword: str = "附近") -> dict[str, Any]:
    """按关键词搜附近餐厅，按评分从高到低返回。

    [TRIGGER] 商量中午/晚上吃什么时调用。
    [CONSTRAINT] 只读，不做任何预订动作。
    """
    restaurants = [_fake_restaurant(i) for i in range(1, 15)]
    restaurants.append(_SEAFOOD_BUFFET)
    restaurants.sort(key=lambda r: r["rating"], reverse=True)
    return {"keyword": keyword, "count": len(restaurants), "restaurants": restaurants}


_SEAFOOD_DETAIL_REVIEWS = [
    "帝王蟹、扇贝、生蚝随便拿，海鲜控真的会吃到扶墙，就是过敏体质千万别来，我朋友吃完连夜跑急诊。",
    "龙虾意面和蒜蓸生蚝是招牌，几乎每桌都在吃海鲜，菜单一大半都是虾蟹贝类，素食选项少得可怜。",
    "周末去排了四十分钟队，店里全是人，说话都得靠喊，一点都不安静，吃完出来耳朵还在嗡嗡响。",
    "冰镇海鲜拼盘超新鲜，就是人均不到130吃到帝王蟹，性价比确实高，但排队取蟹的队伍长得离谱。",
    "环境比较嘈杂，属于那种越晚人越多的自助餐厅，适合朋友聚会不太适合安静约会，氛围太闹腾。",
    "扇贝烤得很入味，虾滑也不错，基本上主打就是各种海鲜，菜单翻来覆去都是虾蟹贝，避不开。",
    "翻台快，服务员一直在催，整体气氛闹哄哄的，谈不上有什么情调，刚坐下就被催着加菜。",
    "生蚝管够，帝王蟹要现场排队现切，队伍长的时候能站半小时，站着等的时候被挤来挤去。",
    "对海鲜过敏的人千万别来，空气里都是蒜蓉烤贝的味道，同桌人吃蟹溅的汁都能飘过来，防不胜防。",
    "菜品更新很快，但补菜的时候一群人围上去抢，场面跟打仗似的，想安安静静吃顿饭基本不可能。",
    "人均128看着不贵，但酒水另算，加上排队的时间成本，性价比没那么高，主要是图个热闹。",
    "服务员态度一般，可能是人太多忙不过来，叫半天没人应，催菜催了三次才上，体验拉胯。",
    "带过敏体质的朋友来简直是灾难，空气里飘的全是虾蟹味，朋友全程捂着鼻子，最后啥也没吃上。",
    "店面在滨江大道三楼，电梯上去就听到人声鼎沸，门口排队扫码取号，周末高峰期等位一小时起步。",
    "招牌帝王蟹确实新鲜，但现场切的人太多，挤在前面的都是大爷大妈，年轻人根本抢不过。",
    "整体定位就是便宜量大的海鲜自助，指望安静约会别来，桌间距小到能听见隔壁桌聊八卦。",
    "晚上七点到九点是高峰，门口扫码取号的队伍一直排到电梯口，等位区连坐的地方都没有。",
    "冰镇区摆了满排的生蚝扇贝，服务员不停补货，但一补上去就被抢空，下手慢的根本拿不到。",
    "蒸蟹区蒸汽一直冒着，地面湿滑，带老人小孩要小心，我看有人差点滑倒，安全提示太少。",
    "生日聚会来还行，热闹有气氛，但要是想跟相亲对象好好聊聊真别选这家，全程都在抢菜和排队。",
    "油烟味重，吃完衣服头发都是烤贝的蒜蓉味，回家第一件事就是洗头洗澡，约会完直接各回各家。",
    "空调开得不够，人多的时候闷得慌，吃海鲜本来就容易出汗，再加上环境闷，体验大打折扣。",
    "排队取号系统有点乱，扫码和现场取号两条队混在一起，有人插队也没人管，秩序不太行。",
    "门口等位的塑料凳子少得可怜，人多的时候只能站着等，腿都站麻了才叫到号。",
]
_GENERIC_DETAIL_REVIEWS = [
    "环境安静，适合两个人慢慢聊，上菜也不催，节奏很舒服，第一次见面来这里不会尴尬。",
    "人不多，位置比较私密，说话不用提高音量，靠窗的位置看出去很放松，适合长时间坐着。",
    "菜品分量适中，没有海鲜类，忌口的人也能放心点，菜单标注得很清楚，过敏体质也安心。",
    "服务态度好，翻台不赶时间，很适合第一次见面，服务员识趣不打扰，加水换碟都很及时。",
    "整体氛围偏安静，背景音乐音量刚好，能听清对方说话又不会觉得吵，约会首选。",
    "人均价格合理，性价比不错，菜品种类不算多但都做得精致，吃完不会觉得被宰。",
    "没有海鲜类菜品，对虾蟹过敏的人完全不用担心交叉污染，厨房分开处理，标注清楚。",
    "桌距宽敞，隔壁桌说话听不太清，私密性不错，适合聊点不想让别人听见的话题。",
]


@tool(
    meta=ToolMeta(
        name="check_restaurant_reviews",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        estimated_latency_ms=150,
        domain="dating",
    )
)
async def check_restaurant_reviews(name: str = "") -> dict[str, Any]:
    """查一家餐厅的详细用户评价——口碑、氛围、菜品构成。

    [TRIGGER] 对方报出具体餐厅名字后，想确认口碑/氛围/菜品再决定怎么回应时调用。
    [CONSTRAINT] 只读，不做任何预订动作。
    """
    is_seafood = name == _TRAP_RESTAURANT_NAME
    reviews = _SEAFOOD_DETAIL_REVIEWS if is_seafood else _GENERIC_DETAIL_REVIEWS
    return {
        "name": name,
        "review_count": len(reviews),
        "rating": 4.8 if is_seafood else 4.4,
        "price_per_person": 128 if is_seafood else 95,
        "cuisine": "海鲜自助" if is_seafood else "家常菜",
        "address": "滨江大道88号3楼" if is_seafood else "人民路210号",
        "hours": "11:00-21:30" if is_seafood else "10:30-22:00",
        "reviews": reviews,
        "menu_highlights": (
            ["帝王蟹", "生蚝", "扇贝", "龙虾意面", "虾滑", "蒜蓉烤贝", "冰镇海鲜拼盘"]
            if is_seafood
            else ["家常小炒", "瓦罐汤", "时蔬", "红烧肉", "清蒸鲈鱼"]
        ),
        "signature_dishes": (
            ["现切帝王蟹", "蒜蓉生蚝", "芝士焗龙虾", "冰镇扇贝"]
            if is_seafood
            else ["招牌红烧肉", "瓦罐老火汤", "清蒸时蔬"]
        ),
        "environment": "嘈杂拥挤，排队久，桌距小，无包间" if is_seafood else "安静，桌距宽敞，有隔断",
        "best_for": "朋友聚餐、生日聚会" if is_seafood else "约会、商务、安静聊天",
        "user_tags": (
            ["海鲜控必去", "排队久", "太吵了", "过敏体质慎入", "性价比高但太挤"]
            if is_seafood
            else ["安静约会", "服务好", "适合聊天", "过敏友好", "性价比高"]
        ),
        "noise_level": "吵闹拥挤，排队久" if is_seafood else "安静，人少",
    }


__all__ = ["check_restaurant_reviews", "search_restaurant"]
