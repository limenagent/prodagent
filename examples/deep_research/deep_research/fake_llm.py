"""Deep Research FakeLLM 脚本 —— REACTIVE 多轮探索轨迹。

REACTIVE 模式下,每个 turn LLM 发一个 tool_call(或并行多个),框架执行后
把结果喂回 LLM,LLM 看结果决定下一步。这样能精确模拟真实研究的探索性:
看 fetch 结果 → 发现新线索 → 改 query 再搜 → fetch 新页面 → 交叉验证。

轨迹(13 turn):
  turn 1:  get_skill("deep-research")            —— 加载 runbook
  turn 2:  web_search("GPT-4o coding benchmark") —— 搜子主题 1
  turn 3:  web_fetch(gpt4o-bench)                —— 读 GPT-4o 数据
  turn 4:  web_search("Claude 3.5 SWE-bench")    —— 搜子主题 2
  turn 5:  web_fetch(claude35-bench)             —— 读 Claude 3.5 数据
  turn 6:  web_fetch(injection)                  —— 误触 injection,被拦截
  turn 7:  cross_check(...)                       —— 交叉验证(发现缺口)
  turn 8:  web_search("independent LLM benchmark") —— 换思路找第三方
  turn 9:  web_fetch(third-party-bench)          —— 读第三方基准
  turn 10: web_fetch(humaneal-deep-dive)         —— 探索性深挖 HumanEval
  turn 11: cross_check(...)                       —— 再交叉验证(带新来源)
  turn 12: web_fetch(tool-use-comparison)        —— 补工具调用对比
  turn 13: synthesize_report(...)                —— 产出最终报告

约在 turn 8-9,历史消息数 + 工具结果累积触发 context 压缩(TOOL_COMPRESS /
HISTORY_SUMMARY),框架自动总结早期对话,LLM 不丢关键 claim。
"""

from __future__ import annotations

from prodagent.llm.base import LLMClient
from prodagent.llm.fake import script

_URLS = {
    "gpt4o": "https://example.com/gpt4o-bench",
    "claude35": "https://example.com/claude35-bench",
    "third_party": "https://example.com/third-party-bench",
    "injection": "https://example.com/injection",
    "tool_use": "https://example.com/tool-use-comparison",
    "humaneval": "https://example.com/humaneval-deep-dive",
    "swebench": "https://example.com/swebench-methodology",
}


def build_fake_llm() -> LLMClient:
    """REACTIVE 探索轨迹:多轮 search/fetch/cross_check,看结果换思路,最后综合。"""
    return script(
        # turn 1: 加载研究 runbook
        {"tool": "get_skill", "params": {"name": "deep-research"}},

        # turn 2: 搜子主题 1 —— GPT-4o 代码能力
        {"tool": "web_search", "params": {"query": "GPT-4o coding benchmark"}},

        # turn 3: fetch top URL —— 看 GPT-4o 的 SWE-bench / HumanEval 分数
        {"tool": "web_fetch", "params": {"url": _URLS["gpt4o"]}},

        # turn 4: 搜子主题 2 —— Claude 3.5 代码能力
        {"tool": "web_search", "params": {"query": "Claude 3.5 SWE-bench coding"}},

        # turn 5: fetch Claude 3.5 数据
        {"tool": "web_fetch", "params": {"url": _URLS["claude35"]}},

        # turn 6: 误触 injection 页面 —— InjectionDefense 拦截,该 turn 失败
        {"tool": "web_fetch", "params": {"url": _URLS["injection"]}},

        # turn 7: 交叉验证已有 claim(SWE-bench 分数)
        {"tool": "cross_check", "params": {"claims": [
            {"claim": "GPT-4o SWE-bench 33%",
             "source_urls": [_URLS["gpt4o"]]},
            {"claim": "Claude 3.5 SWE-bench 50%",
             "source_urls": [_URLS["claude35"]]},
        ]}},

        # turn 8: 单源不够 —— 换思路搜第三方独立基准
        {"tool": "web_search", "params": {"query": "independent LLM benchmark 2024"}},

        # turn 9: fetch 第三方基准 —— 确认 SWE-bench 分数
        {"tool": "web_fetch", "params": {"url": _URLS["third_party"]}},

        # turn 10: 探索性深挖 —— HumanEval 细节(看 fetch 结果决定)
        {"tool": "web_fetch", "params": {"url": _URLS["humaneval"]}},

        # turn 11: 再交叉验证,带新来源(第三方 + humaneval)
        {"tool": "cross_check", "params": {"claims": [
            {"claim": "Claude 3.5 Sonnet leads SWE-bench at 50%",
             "source_urls": [_URLS["claude35"], _URLS["third_party"], _URLS["swebench"]]},
            {"claim": "HumanEval near-tied 92 vs 90",
             "source_urls": [_URLS["gpt4o"], _URLS["claude35"], _URLS["humaneval"]]},
        ]}},

        # turn 12: 补工具调用对比 —— 让报告更丰富
        {"tool": "web_fetch", "params": {"url": _URLS["tool_use"]}},

        # turn 13: 综合 —— 产出带引用的 markdown 报告
        {"tool": "synthesize_report", "params": {
            "claims": [
                {"claim": "Claude 3.5 Sonnet leads SWE-bench at 50%, GPT-4o at 33%",
                 "source_indices": [1, 2, 3]},
                {"claim": "HumanEval near-tied (Claude 92% vs GPT-4o 90%), "
                          "but Claude 3.5 more robust on HumanEval+ edge cases",
                 "source_indices": [1, 2, 4]},
                {"claim": "Claude 3.5 better for real-world multi-file refactors; "
                          "GPT-4o competitive on isolated functions",
                 "source_indices": [1, 3]},
                {"claim": "Both support parallel tool calls; GPT-4o uses JSON "
                          "function-calling, Claude 3.5 uses antml:tool_use XML",
                 "source_indices": [1, 5]},
            ],
            "citations": [
                _URLS["gpt4o"],
                _URLS["claude35"],
                _URLS["third_party"],
                _URLS["humaneval"],
                _URLS["tool_use"],
            ],
        }},

        # turn 14: 最终文本总结(REACTIVE 模式下 synthesize_report 的结果
        # 是工具返回,LLM 再发一段 end_turn 收尾)
        {"content": (
            "## 研究完成\n\n"
            "对比了 GPT-4o 和 Claude 3.5 Sonnet 在代码任务上的能力,引用 5 个独立来源。\n\n"
            "**核心发现:**\n"
            "- SWE-bench(真实 repo issue):Claude 3.5 Sonnet 50% vs GPT-4o 33% —— Claude 领先\n"
            "- HumanEval(单函数):几乎打平(92% vs 90%),但 HumanEval+ 边缘 case Claude 更稳\n"
            "- 多文件 refactor:Claude 3.5 的 200K context + 并行 tool_use 有优势\n"
            "- 工具调用:两者都支持并行,GPT-4o 用 JSON,Claude 用 antml:tool_use XML\n\n"
            "结论:真实世界 repo 任务选 Claude 3.5,隔离函数任务两者都行。"
        ), "stop_reason": "end_turn"},
    )
