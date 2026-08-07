"""Deep Research 工具 —— fetch + 综合。

``web_fetch`` 读内存假 web(不连真实网络),结果内联进 context(``max_result_chars=inf``),
LLM 直接读 content 字段里的 benchmark 数字,不需要 ``read_tool_result`` 回捞。

``synthesize_report`` 把验证过的 claims + citations 组成带内联引用的 markdown 报告。
"""

from __future__ import annotations

from prodagent import ErrorReason, SideEffectLevel, ToolMeta, tool
from prodagent.core.types import ToolError

# ── 假 web —— mock 数据,demo 不依赖外网 ───────────────────────────────────────

_FAKE_WEB: dict[str, str] = {
    "https://example.com/gpt4o-bench": (
        "# GPT-4o Coding Benchmark Report\n\n"
        "## Overview\n"
        "GPT-4o was released 2024-05 as OpenAI's flagship multimodal model. "
        "This report covers its performance on established coding benchmarks "
        "with detailed methodology, statistical analysis, and comparison to "
        "prior GPT-4 Turbo releases. The model supports 128K context, "
        "structured tool calls, and multimodal input including code "
        "screenshots and architecture diagrams. We focus here on text-in / "
        "text-out coding tasks where the model's reasoning chain can be "
        "compared fairly against Claude 3.5 Sonnet on identical prompts.\n\n"
        "## Key Results\n"
        "- SWE-bench: 33% resolved (verified subset)\n"
        "- HumanEval pass@1: 90%\n"
        "- HumanEval+ pass@1: 86%\n"
        "- Context window: 128K tokens\n"
        "- Coding strengths: Python, TypeScript, JavaScript\n"
        "- Weakness: long-refactor tasks, multi-file dependency tracking\n\n"
        "## Methodology\n"
        "We evaluated GPT-4o on SWE-bench Verified, a curated subset of 500 "
        "real GitHub issues from 12 popular Python repositories including "
        "django, scikit-learn, matplotlib, and sympy. Each task requires the "
        "model to produce a patch that resolves the issue and passes the "
        "repo's test suite. We ran each task 3 times with temperature 0.2 "
        "and report the pass rate. Failures were categorized by root cause: "
        "wrong API usage, missing edge-case handling, breaking adjacent "
        "tests, or incomplete patches. Inter-rater agreement on failure "
        "categories was 0.87 (Cohen's kappa).\n\n"
        "## Detailed Breakdown\n"
        "On SWE-bench, GPT-4o resolved 165 out of 500 issues (33%). Breaking "
        "down by difficulty: easy 58%, medium 28%, hard 12%. The model "
        "struggled particularly with issues requiring understanding of "
        "cross-module data flow, where it often produced patches that fixed "
        "the immediate symptom but broke adjacent tests. On HumanEval, "
        "GPT-4o achieved 90.2% pass@1 on the standard 164-problem suite, "
        "near saturation. However on HumanEval+ (extended test cases that "
        "probe edge conditions), it dropped to 86%, suggesting brittleness "
        "on boundary conditions. Per-category breakdown: list ops 98%, "
        "string ops 95%, math/algorithm 88%, recursion 85%, dynamic "
        "programming 76%, graph algorithms 72%.\n\n"
        "## Tool Use\n"
        "GPT-4o supports parallel function calling via JSON schema. The "
        "model can emit multiple tool calls in a single response, enabling "
        "concurrent file reads, test runs, and searches. Median tool-call "
        "latency is 1.2s. The function-calling format uses a structured "
        "JSON envelope with name, arguments, and call_id fields. On "
        "SWE-bench, GPT-4o averaged 11.4 tool calls per resolved task, "
        "compared to 8.3 for Claude 3.5 Sonnet — suggesting GPT-4o needs "
        "more exploration to converge on a correct patch.\n\n"
        "## Limitations\n"
        "On multi-file refactors spanning 5+ files, GPT-4o's success rate "
        "drops to 18%. The 128K context, while large, can be consumed "
        "quickly when the model must read multiple large source files. "
        "We observed context exhaustion in 12% of multi-file tasks, where "
        "the model began dropping early file contents from its attention. "
        "On tasks requiring knowledge of framework internals (e.g. Django "
        "ORM internals), GPT-4o sometimes hallucinated API methods that "
        "do not exist, leading to runtime errors in the patched code.\n\n"
        "## Cost and Latency\n"
        "Per-task cost on SWE-bench: $0.008 average (input + output tokens "
        "at list price). Median wall-clock time per resolved task: 47s. "
        "First-token latency 1.1s. Throughput approximately 21 tokens/sec "
        "during generation. Compared to Claude 3.5 Sonnet, GPT-4o is "
        "cheaper per task but resolves fewer issues, leading to similar "
        "cost-per-success on SWE-bench overall.\n\n"
        "## References\n"
        "[1] SWE-bench: Can Language Models Resolve Real-World GitHub Issues?\n"
        "[2] HumanEval: Evaluating Code Generation on Functional Correctness\n"
        "[3] OpenAI GPT-4o Technical Report, 2024"
    ),
    "https://example.com/claude35-bench": (
        "# Claude 3.5 Sonnet Coding Benchmark Report\n\n"
        "## Overview\n"
        "Claude 3.5 Sonnet was released 2024-06 by Anthropic. It achieved "
        "the highest SWE-bench score at release time and demonstrated "
        "strong performance across coding, reasoning, and tool-use tasks. "
        "This report provides detailed benchmark results and analysis, "
        "with emphasis on multi-file refactor performance where the model "
        "shows the largest lead over GPT-4o. The model supports a 200K "
        "context window and uses antml:tool_use XML format for structured "
        "tool invocation.\n\n"
        "## Key Results\n"
        "- SWE-bench Verified: 50% (highest at release)\n"
        "- HumanEval pass@1: 92%\n"
        "- HumanEval+ pass@1: 89%\n"
        "- Context window: 200K tokens\n"
        "- Strength: multi-file refactors, agentic tool use\n"
        "- Tool format: antml:tool_use XML, native parallel calls\n\n"
        "## Methodology\n"
        "We evaluated Claude 3.5 Sonnet on the same SWE-bench Verified "
        "subset of 500 real GitHub issues. Each task was run 3 times at "
        "temperature 0.2. The model was given access to file_read, "
        "file_edit, and run_test tools. We measured both resolution rate "
        "and the number of tool calls required. To control for prompt "
        "sensitivity, three prompt phrasings were tested per task; the "
        "median pass rate across phrasings is reported. Inter-rater "
        "agreement on failure categorization was 0.91 (Cohen's kappa), "
        "higher than GPT-4o's 0.87 due to fewer ambiguous 'wrong root "
        "cause' cases.\n\n"
        "## Detailed Breakdown\n"
        "Claude 3.5 Sonnet resolved 250 out of 500 SWE-bench issues (50%). "
        "By difficulty: easy 72%, medium 48%, hard 22%. The model excelled "
        "at issues requiring multi-file understanding, maintaining coherent "
        "edits across 5+ files with 41% success rate. On HumanEval it "
        "scored 92.0% pass@1, and on HumanEval+ 89%, showing better edge-"
        "case robustness than GPT-4o. The 200K context window proved "
        "advantageous on large-repo tasks where GPT-4o exhausted context. "
        "Per-category HumanEval breakdown: list ops 99%, string ops 96%, "
        "math/algorithm 91%, recursion 89%, dynamic programming 82%, "
        "graph algorithms 78%.\n\n"
        "## Tool Use\n"
        "Claude 3.5 Sonnet uses antml:tool_use XML tags for tool invocation, "
        "enabling native parallel tool calls in a single response. The "
        "model demonstrated effective tool-chaining, averaging 8.3 tool "
        "calls per SWE-bench task. Median tool latency 1.2s, comparable "
        "to GPT-4o. The XML format provides structured tool name, "
        "parameters, and call identification. On multi-file tasks, Claude "
        "3.5 commonly batches 3-5 file reads in parallel before planning "
        "edits, a pattern GPT-4o employs less consistently.\n\n"
        "## Strengths\n"
        "Multi-file refactors: 41% success on 5+ file tasks vs GPT-4o's "
        "18%. Long-context comprehension: maintains accuracy at 150K+ "
        "tokens where GPT-4o degrades. Agentic planning: decomposes "
        "complex issues into ordered subtasks without explicit prompting. "
        "Better null/None handling on HumanEval+ (31% of failures vs "
        "GPT-4o's 38%). Lower rate of hallucinated API calls (3% vs 9%).\n\n"
        "## Limitations\n"
        "Occasional over-engineering: produces patches more complex than "
        "necessary (12% of resolved tasks had reviewer-flagged unnecessary "
        "abstractions). Slower first-token latency (1.8s vs GPT-4o's 1.1s). "
        "Higher cost per token at list price. On pure single-function "
        "algorithmic tasks (no tool use), the lead over GPT-4o narrows to "
        "2 points on HumanEval, within statistical noise.\n\n"
        "## Cost and Latency\n"
        "Per-task cost on SWE-bench: $0.012 average. Median wall-clock "
        "time per resolved task: 52s. First-token latency 1.8s. Throughput "
        "approximately 18 tokens/sec during generation. Despite higher "
        "per-task cost, the 52% higher resolution rate yields comparable "
        "cost-per-success ($0.024 vs $0.024). On multi-file tasks, Claude "
        "3.5 is 2.3x more cost-effective due to fewer failed attempts.\n\n"
        "## References\n"
        "[1] Anthropic Claude 3.5 Sonnet Technical Report, 2024\n"
        "[2] SWE-bench Verified Leaderboard\n"
        "[3] HumanEval Benchmark Suite"
    ),
    "https://example.com/third-party-bench": (
        "# Independent LLM Coding Benchmark 2024-Q4\n\n"
        "## Summary\n"
        "This independent benchmark, conducted by a third-party evaluation "
        "lab, compares leading LLMs on coding tasks as of 2024-Q4. We "
        "include SWE-bench, HumanEval, HumanEval+, and a custom multi-file "
        "refactor suite. All models were evaluated under identical "
        "conditions with no vendor affiliation. Evaluation code and raw "
        "outputs are open-sourced for reproducibility. The lab accepts no "
        "funding from any model vendor; costs were covered by a neutral "
        "industry consortium.\n\n"
        "## Results Table\n"
        "| Model           | SWE-bench | HumanEval | HumanEval+ | Multi-file |\n"
        "| Claude 3.5 Smt  | 50%       | 92%       | 89%        | 41%        |\n"
        "| GPT-4o          | 33%       | 90%       | 86%        | 18%        |\n"
        "| GPT-4 Turbo     | 22%       | 85%       | 80%        | 10%        |\n"
        "| Claude 3 Opus   | 18%       | 83%       | 77%        | 8%         |\n\n"
        "## Conclusion\n"
        "Claude 3.5 Sonnet leads SWE-bench at 50%, GPT-4o at 33%. HumanEval "
        "near-tied (92 vs 90), but HumanEval+ shows Claude 3.5 more robust "
        "to edge cases (89 vs 86). Claude 3.5 better for real-world repo "
        "tasks; GPT-4o competitive on isolated functions. The gap widens "
        "significantly on multi-file refactors where Claude 3.5's 200K "
        "context and parallel tool use provide structural advantages. "
        "Claude 3 Opus and GPT-4 Turbo trail on all fronts, indicating "
        "the 3.5 / 4o generation represents a real capability jump.\n\n"
        "## Methodology\n"
        "All models evaluated via their respective APIs at temperature 0.2. "
        "SWE-bench Verified subset (500 tasks). HumanEval (164 problems) "
        "and HumanEval+ (extended test cases). Multi-file refactor suite "
        "includes 100 tasks requiring edits across 5-15 files. Each task "
        "run 3 times; pass rate reported. No vendor involvement or "
        "sponsorship. Evaluation code and raw results are open-sourced. "
        "Three prompt phrasings per task; median reported to control for "
        "prompt sensitivity.\n\n"
        "## Statistical Notes\n"
        "Differences exceeding 3 percentage points on SWE-bench are "
        "statistically significant (p<0.05, bootstrap n=1000). The Claude "
        "3.5 vs GPT-4o gap of 17 points is highly significant (p<0.001). "
        "On HumanEval, the 2-point gap is within noise (p=0.12). HumanEval+ "
        "3-point gap is borderline significant (p=0.06). Multi-file "
        "refactor gap (41% vs 18%) is highly significant (p<0.001). All "
        "confidence intervals computed via bootstrap with 1000 resamples.\n\n"
        "## Cost Analysis\n"
        "Per-task cost: Claude 3.5 $0.012 avg, GPT-4o $0.008 avg. Claude "
        "3.5 costs 50% more per task but resolves 52% more issues, yielding "
        "similar cost-per-success on SWE-bench ($0.024 vs $0.024, tied). "
        "On multi-file tasks, Claude 3.5 is 2.3x more cost-effective due "
        "to fewer failed retries. GPT-4 Turbo at $0.006/task is cheapest "
        "but its 22% resolution makes cost-per-success $0.027, worse than "
        "both front-runners. Cost analysis uses list pricing as of "
        "2024-12-01; volume discounts not considered.\n\n"
        "## Reproducibility\n"
        "All prompts, evaluation scripts, and raw model outputs are "
        "published at github.com/example/llm-code-bench. The evaluation "
        "harness is pinned to specific model versions: claude-3-5-sonnet-"
        "20241022, gpt-4o-2024-08-06, gpt-4- turbo-2024-04-09, claude-3-"
        "opus-20240229. Re-running with these exact version strings "
        "reproduces the reported numbers within +/- 1 percentage point.\n\n"
        "## References\n"
        "[1] SWE-bench: Can Language Models Resolve Real-World GitHub Issues?\n"
        "[2] HumanEval and HumanEval+ benchmark suites\n"
        "[3] Open evaluation framework: github.com/example/llm-code-bench"
    ),
    "https://example.com/tool-use-comparison": (
        "# LLM Tool Use Comparison: GPT-4o vs Claude 3.5 Sonnet\n\n"
        "## Overview\n"
        "This report compares the tool-use capabilities of GPT-4o and "
        "Claude 3.5 Sonnet, covering format, parallelism, latency, and "
        "ergonomics. Both models support parallel tool calls but differ "
        "in wire format and developer experience. We measure throughput, "
        "error rates, and integration friction across 50 tool-heavy "
        "agentic tasks per model. The goal is to give framework builders "
        "concrete data on which format is easier to integrate robustly.\n\n"
        "## GPT-4o Tool Use\n"
        "GPT-4o uses JSON function-calling. The developer defines tools "
        "as JSON Schema objects; the model emits a structured response "
        "with name, arguments (JSON), and a call_id. Multiple tool calls "
        "can appear in one response (parallel). The format is:\n"
        '  {"name": "search", "arguments": {"q": "..."}, "id": "call_123"}\n'
        "Strengths: JSON is ubiquitous, easy to parse, works with "
        "existing OpenAI SDK ecosystem. Weaknesses: JSON escaping for "
        "complex nested params can be verbose; no native streaming of "
        "partial tool args. Schema validation errors surface only after "
        "the full response is received, increasing latency to first "
        "retries on malformed calls.\n\n"
        "## Claude 3.5 Sonnet Tool Use\n"
        "Claude 3.5 uses antml:tool_use XML tags. The model emits:\n"
        '  <antml:tool_use name="search" id="toolu_123">\n'
        "    <parameters><q>...</q></parameters>\n"
        "  </antml:tool_use>\n"
        "Multiple tool_use blocks can appear in one response (parallel). "
        "Strengths: XML is more readable for nested params; native "
        "streaming of tool name + args; explicit parameter tags reduce "
        "ambiguity. Weaknesses: more verbose for simple calls; XML "
        "parsing slightly heavier than JSON. Schema errors can be "
        "detected mid-stream, allowing earlier retry.\n\n"
        "## Parallelism\n"
        "Both models support parallel tool calls. GPT-4o emits an array "
        "of tool_call objects. Claude 3.5 emits multiple tool_use blocks "
        "in sequence within one response. In practice, both achieve "
        "similar throughput for concurrent readonly tools (file reads, "
        "searches). For write tools, both default to sequential unless "
        "explicitly told parallel is safe. On the 50-task agentic suite, "
        "GPT-4o averaged 1.8 parallel calls per turn, Claude 3.5 averaged "
        "2.4 — Claude 3.5 batches more aggressively when the task "
        "structure allows it.\n\n"
        "## Latency\n"
        "Median tool-call round-trip: GPT-4o 1.2s, Claude 3.5 1.2s. "
        "First-token latency: GPT-4o 1.1s, Claude 3.5 1.8s. Claude 3.5 "
        "is slower to start but similar once streaming begins. For "
        "tool-heavy workflows, the first-token gap matters less because "
        "subsequent calls pipeline. p99 round-trip: GPT-4o 2.4s, Claude "
        "3.5 3.1s — Claude 3.5 has a longer tail, partly due to the "
        "longer first-token latency compounding across multi-turn flows.\n\n"
        "## Developer Experience\n"
        "GPT-4o's JSON format integrates with existing OpenAI SDK and "
        "LangChain/LlamaIndex patterns. Claude 3.5's XML format requires "
        "Anthropic SDK or custom parsing, but the explicit parameter "
        "tags reduce schema-validation bugs. Both support streaming, "
        "though Claude 3.5's native tool-arg streaming is smoother. "
        "In a developer survey (n=42 framework maintainers), GPT-4o's "
        "JSON format scored 4.2/5 on integration ease, Claude 3.5's XML "
        "scored 3.9/5; the gap narrows to 4.1 vs 4.0 among developers "
        "who had used both for 3+ months.\n\n"
        "## Error Rates\n"
        "On the 50-task suite: GPT-4o had 8% malformed tool calls "
        "(missing required arg, wrong type), Claude 3.5 had 4%. Retry "
        "overhead: GPT-4o added 14% to wall-clock time on average, "
        "Claude 3.5 added 7%. The lower malformed-call rate on Claude "
        "3.5 is attributed to the explicit parameter tags reducing "
        "ambiguity on nested schemas.\n\n"
        "## Conclusion\n"
        "Both models support parallel tool calls; GPT-4o uses JSON "
        "function-calling, Claude 3.5 uses antml:tool_use XML. Latency "
        "similar (~1.2s median). Choice often comes down to ecosystem "
        "fit: OpenAI-compatible stacks prefer GPT-4o's JSON; Anthropic "
        "stacks prefer Claude 3.5's XML for its explicit structure and "
        "lower malformed-call rate."
    ),
    "https://example.com/humaneval-deep-dive": (
        "# HumanEval Deep Dive: GPT-4o vs Claude 3.5 Sonnet\n\n"
        "## Introduction\n"
        "HumanEval is a benchmark of 164 hand-written Python programming "
        "problems testing functional correctness. Each problem has a "
        "function signature, docstring, and test cases. The model must "
        "complete the function body. This deep dive analyzes GPT-4o and "
        "Claude 3.5 Sonnet performance on HumanEval and the extended "
        "HumanEval+ suite. We also break down results by problem category "
        "and analyze failure modes to explain the small but consistent "
        "Claude 3.5 lead on edge-case-heavy tasks.\n\n"
        "## Standard HumanEval Results\n"
        "- GPT-4o pass@1: 90.2%\n"
        "- Claude 3.5 Sonnet pass@1: 92.0%\n"
        "Both near saturation. The 1.8-point gap is within statistical "
        "noise (p=0.12, bootstrap). On the standard suite, both models "
        "reliably solve list manipulation, string processing, and basic "
        "algorithm problems. The gap appears consistently across three "
        "prompt phrasings tested, suggesting it is a real (if small) "
        "capability difference rather than a prompt artifact.\n\n"
        "## HumanEval+ Results\n"
        "HumanEval+ extends each problem with additional test cases "
        "probing edge conditions: empty inputs, boundary values, type "
        "coercion, and adversarial inputs.\n"
        "- GPT-4o pass@1: 86% (4-point drop from HumanEval)\n"
        "- Claude 3.5 Sonnet pass@1: 89% (3-point drop)\n"
        "Claude 3.5 is more robust to edge cases. The 3-point gap on "
        "HumanEval+ is borderline significant (p=0.06). The drop from "
        "HumanEval to HumanEval+ is smaller for Claude 3.5 (3 points) "
        "than GPT-4o (4 points), indicating better handling of boundary "
        "conditions the standard suite does not probe.\n\n"
        "## Failure Analysis\n"
        "GPT-4o failures on HumanEval+ cluster around: null/None "
        "handling (38% of failures), off-by-one errors (24%), type "
        "coercion (18%), integer overflow (12%), other (8%). Claude "
        "3.5 failures: null/None (31%), off-by-one (20%), type coercion "
        " (22%), integer overflow (10%), other (17%). Claude 3.5 shows "
        "slightly better null handling but similar type-coercion issues. "
        "The 'other' category is larger for Claude 3.5, partly because "
        "its failures are more idiosyncratic and less clustered around "
        "common bug patterns.\n\n"
        "## Problem Categories\n"
        "| Category          | GPT-4o | Claude 3.5 |\n"
        "| List ops          | 98%    | 99%        |\n"
        "| String ops        | 95%    | 96%        |\n"
        "| Math/algorithm    | 88%    | 91%        |\n"
        "| Recursion         | 85%    | 89%        |\n"
        "| Dynamic prog      | 76%    | 82%        |\n"
        "| Graph algorithms  | 72%    | 78%        |\n\n"
        "Claude 3.5 leads in algorithm-heavy categories, especially "
        "dynamic programming and graphs where longer reasoning chains "
        "help. Both models saturate on list/string ops. The 6-point "
        "gap on dynamic programming is the largest category-level "
        "difference and is statistically significant (p<0.05).\n\n"
        "## Context Window Impact\n"
        "We tested with varying context loads. At 50K context used, "
        "both models maintain HumanEval accuracy. At 100K, GPT-4o drops "
        "2 points (88%), Claude 3.5 stable (92%). At 150K, GPT-4o drops "
        "to 82%, Claude 3.5 to 90%. Claude 3.5's 200K window gives "
        "better long-context coding robustness. Note: HumanEval problems "
        "are short, so context load here is synthetic (padded with "
        "irrelevant tokens) to probe long-context degradation in a "
        "controlled way.\n\n"
        "## Prompt Sensitivity\n"
        "Three prompt phrasings tested per problem: minimal (signature "
        "+ docstring), explanatory (signature + docstring + hint), and "
        "adversarial (signature + docstring + misleading hint). GPT-4o "
        "showed 4-point variance across phrasings, Claude 3.5 showed "
        "2-point variance. Claude 3.5 is more robust to misleading "
        "hints, recovering to a correct solution in 89% of adversarial-"
        "prompt cases vs GPT-4o's 81%.\n\n"
        "## Conclusion\n"
        "HumanEval near-tied (GPT-4o 90% vs Claude 3.5 92%), but "
        "HumanEval+ shows Claude 3.5 more robust to edge cases (89 vs "
        "86). The category breakdown reveals the lead concentrates in "
        "algorithm-heavy problems (dynamic programming, graphs) where "
        "longer reasoning chains help. See also third-party-bench for "
        "SWE-bench comparison."
    ),
}


# ── web_fetch —— mock,读内存页面 ───────────────────────────────────────────────


@tool(
    meta=ToolMeta(
        name="web_fetch",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        estimated_latency_ms=50,
        domain="research",
        max_result_chars=float("inf"),
    )
)
async def web_fetch(url: str) -> dict:
    """抓取一个 web 页面的完整内容。

    [TRIGGER] 拿到 URL 后读全文。多个 URL 用并行调用。
    [CONSTRAINT] 只读;每次调用一个 URL;URL 必须是 ``example.com/*`` 下的页面
    (本环境是 mock web)。fetch 结果直接进 context,读 content 字段,不需要
    ``read_tool_result`` 回捞。

    Args:
        url: 要抓取的 URL。
    """
    content = _FAKE_WEB.get(url)
    if content is None:
        return ToolError.from_reason(
            ErrorReason.FORMAT_ERROR,
            code="not_found",
            message=f"404 not found: {url!r}. Only example.com/* URLs exist in the mock web.",
            hint="Use one of the URLs listed in the system prompt.",
        )
    return {"url": url, "content": content, "chars": len(content), "ok": True}


# ── synthesize_report —— 把验证过的 claims 组成带引用的 markdown ───────────────


@tool(
    meta=ToolMeta(
        name="synthesize_report",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        reversibility=0.95,
        estimated_latency_ms=100,
        domain="research",
    )
)
async def synthesize_report(claims: list[dict], citations: list[str]) -> dict:
    """把验证过的 claims 组成带内联引用的 markdown 报告。

    [TRIGGER] fetch 完所有来源后,产出最终报告。
    [CONSTRAINT] 只读;citations 必须是实际抓取并使用过的 URL。

    Args:
        claims: 验证过的 claim 列表,每个含 claim 文本和支撑来源序号。
        citations: 引用的 URL 列表(按出现顺序)。
    """
    if not claims:
        return ToolError.from_reason(
            ErrorReason.FORMAT_ERROR,
            code="empty_claims",
            message="claims list is empty.",
            hint="Pass at least one verified claim.",
        )
    lines = ["# Research Report", ""]
    for i, c in enumerate(claims, 1):
        claim_text = c.get("claim", f"claim {i}")
        source_idx = c.get("source_indices", [1])
        refs = ", ".join(f"[{j}]" for j in source_idx)
        lines.append(f"{i}. {claim_text} {refs}")
    lines.append("")
    lines.append("## References")
    for i, url in enumerate(citations, 1):
        title = url.split("/")[-1].replace("-", " ").title()
        lines.append(f"[{i}] {title}. {url}")
    return {"report": "\n".join(lines), "citations": len(citations), "ok": True}


__all__ = ["synthesize_report", "web_fetch"]
