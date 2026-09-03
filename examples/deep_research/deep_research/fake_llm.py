"""Deep Research FakeLLM 脚本 —— ReAct 线性探索轨迹。

ReAct 模式下,每个 turn LLM 发一个 tool_call,框架执行后把结果喂回 LLM,
LLM 看结果决定下一步。

轨迹(8 turn):
  turn 1:  get_skill("deep-research")         —— 加载 runbook
  turn 2:  web_fetch(gpt4o-bench)             —— 读 GPT-4o 数据
  turn 3:  web_fetch(claude35-bench)          —— 读 Claude 3.5 数据
  turn 4:  web_fetch(third-party-bench)       —— 第三方印证
  turn 5:  web_fetch(humaneval-deep-dive)     —— HumanEval 细节
  turn 6:  web_fetch(tool-use-comparison)     —— 工具调用对比
  turn 7:  synthesize_report(...)             —— 产出带引用的报告
  turn 8:  end_turn                            —— 文本总结

约在 turn 3-4,fetch 结果累积触发 TOOL_COMPRESS(工具结果被规则压缩);
turn 4-5 触发 HISTORY_SUMMARY(早期对话被 LLM 总结)。

aux caller(summariser/classifier)不再需要特殊处理:框架的
``ContextManager`` 现在自带独立的 ``aux_llm``(offline 由
``backends.factory.resolve_aux_llm`` 解析),aux call 根本不经过主 LLM,
一个纯 FIFO 的 ``script()`` 队列就够,不会吃掉 scripted turn。
"""

from __future__ import annotations

from prodagent import LLMClient, script

_URLS = {
    "gpt4o": "https://example.com/gpt4o-bench",
    "claude35": "https://example.com/claude35-bench",
    "third_party": "https://example.com/third-party-bench",
    "humaneval": "https://example.com/humaneval-deep-dive",
    "tool_use": "https://example.com/tool-use-comparison",
}


def build_fake_llm() -> LLMClient:
    """ReAct 线性探索:5 次 fetch + 综合 + 总结。"""
    return script(
        # turn 1: 加载研究 runbook
        {"tool": "get_skill", "params": {"name": "deep-research"}},

        # turn 2: fetch GPT-4o 数据
        {"tool": "web_fetch", "params": {"url": _URLS["gpt4o"]}},
        # turn 3: fetch Claude 3.5 数据
        {"tool": "web_fetch", "params": {"url": _URLS["claude35"]}},
        # turn 4: fetch 第三方基准 —— 印证两家数字
        {"tool": "web_fetch", "params": {"url": _URLS["third_party"]}},
        # turn 5: fetch HumanEval 细节
        {"tool": "web_fetch", "params": {"url": _URLS["humaneval"]}},
        # turn 6: fetch 工具调用对比
        {"tool": "web_fetch", "params": {"url": _URLS["tool_use"]}},

        # turn 7: 综合 —— 产出带引用的 markdown 报告
        {"tool": "synthesize_report", "params": {
            "claims": [
                {"claim": "Claude 3.5 Sonnet leads SWE-bench at 50%, GPT-4o at 33%",
                 "source_indices": [1, 2, 3]},
                {"claim": "HumanEval near-tied (Claude 92% vs GPT-4o 90%), "
                          "but Claude 3.5 more robust on HumanEval+ edge cases (89 vs 86)",
                 "source_indices": [1, 2, 4]},
                {"claim": "Claude 3.5 better for multi-file refactors (41% vs 18%); "
                          "GPT-4o competitive on isolated functions",
                 "source_indices": [2, 3]},
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

        # turn 8: 最终文本总结(ReAct 模式下 synthesize_report 的结果
        # 是工具返回,LLM 再发一段 end_turn 收尾)
        {"content": (
            "## 研究完成\n\n"
            "对比了 GPT-4o 和 Claude 3.5 Sonnet 在代码任务上的能力,引用 5 个独立来源。\n\n"
            "**核心发现:**\n"
            "- SWE-bench(真实 repo issue):Claude 3.5 Sonnet 50% vs GPT-4o 33% —— Claude 领先\n"
            "- HumanEval(单函数):几乎打平(92% vs 90%),但 HumanEval+ 边缘 case Claude 更稳(89 vs 86)\n"
            "- 多文件 refactor:Claude 3.5 的 200K context + 并行 tool_use 有优势(41% vs 18%)\n"
            "- 工具调用:两者都支持并行,GPT-4o 用 JSON,Claude 用 antml:tool_use XML\n\n"
            "结论:真实世界 repo 任务选 Claude 3.5,隔离函数任务两者都行。"
        ), "stop_reason": "end_turn"},
    )
