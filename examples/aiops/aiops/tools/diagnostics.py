"""L1 只读诊断工具 —— 可以安全并行执行。"""

from __future__ import annotations

from prodagent import SideEffectLevel, ToolMeta, tool


@tool(
    meta=ToolMeta(
        name="query_metrics",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        estimated_latency_ms=300,
        domain="observability",
    )
)
async def query_metrics(service: str, metric: str, window: str = "5m") -> dict:
    """查一个服务的某个原始指标值，用于偏差分析。

    [TRIGGER] 需要某个具体指标值（错误率、延迟、CPU、内存）与基线对比
    或调查异常时用。
    [MUTEX] 如果只是想看 SLO 状态或剩余 error budget，用 check_slo ——
    不要用 query_metrics 间接算 budget。query_metrics 返回原始时序数据；
    不算 error budget %。
    [CONSTRAINT] 只读；每次调用一个指标 —— 多个指标用并行调用。

    Args:
        service: Kubernetes 服务名，如 payment-service
        metric:  指标名: error_rate | latency_p99 | cpu_usage | memory_rss
        window:  时间窗口: 1m | 5m | 15m | 1h
    """
    _data = {
        "error_rate": {"value": 0.34, "baseline": 0.01},
        "latency_p99": {"value": 4200, "baseline": 180},
        "cpu_usage": {"value": 0.92, "baseline": 0.40},
        "memory_rss": {"value": 3.8, "baseline": 1.2},
    }
    data = _data.get(metric, {"value": 0, "baseline": 0})
    deviation = (data["value"] / data["baseline"]) if data["baseline"] else 0
    return {
        "service": service,
        "metric": metric,
        "value": data["value"],
        "baseline": data["baseline"],
        "deviation": round(deviation, 2),
        "anomalous": deviation > 3.0,
    }


@tool(
    meta=ToolMeta(
        name="tail_logs",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        estimated_latency_ms=500,
        domain="observability",
    )
)
async def tail_logs(service: str, lines: int = 50, grep: str = "") -> dict:
    """拉一个服务的近期日志。

    Args:
        service: Kubernetes 服务名
        lines:   近期日志行数（最多 30）
        grep:    可选过滤模式（服务端应用）
    """
    lines = min(lines, 30)
    _logs = {
        "payment-service": [
            "2026-06-18T02:13:35Z WARN  [order-batch-processor] Heap growing: 1.8GiB / 2.0GiB limit",
            "2026-06-18T02:13:42Z WARN  [order-batch-processor] GC pressure high: 34 collections in 60s",
            "2026-06-18T02:13:50Z ERROR [order-batch-processor] FATAL: allocated 2.4GiB, limit 2.0GiB exceeded",
            "2026-06-18T02:13:51Z ERROR container payment-worker received SIGKILL (exit 137, OOMKilled)",
            "2026-06-18T02:13:57Z WARN  [payment-gateway] upstream circuit breaker tripped: payment-worker",
            "2026-06-18T02:14:19Z ERROR container payment-worker received SIGKILL (exit 137, OOMKilled, count=2)",
            "2026-06-18T02:14:22Z WARN  [payment-api] SLO error budget consuming at 14x normal rate",
            "2026-06-18T02:14:31Z WARN  kubelet: CrashLoopBackOff: pod payment-worker-7d9f8b-mq9r (backoff: 10s)",
            "2026-06-18T02:14:33Z ERROR OOMKilled: container payment-worker exceeded context limit (3.8GiB / 2.0GiB)",
            "2026-06-18T02:14:42Z ERROR [alertmanager] Alert fired: PaymentWorkerCrashLoopBackOff (severity=critical)",
            "2026-06-18T02:14:42Z ERROR [alertmanager] Alert fired: SLOBurnRate14x1Hour (severity=page)",
            "2026-06-18T02:15:02Z WARN  kubelet: pod payment-worker-7d9f8b-mq9r: CrashLoopBackOff (backoff: 40s)",
        ],
        "checkout-service": [
            "2026-06-18T02:15:01Z ERROR upstream connect error: payment-service unavailable",
            "2026-06-18T02:15:02Z WARN  circuit breaker OPEN for payment-service dependency",
        ],
    }
    raw = _logs.get(service, [f"[no logs for {service}]"])
    filtered = [line for line in raw if grep.lower() in line.lower()] if grep else raw
    return {"service": service, "lines": filtered[:lines]}


@tool(
    meta=ToolMeta(
        name="get_pod_status",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        estimated_latency_ms=200,
        domain="kubernetes",
    )
)
async def get_pod_status(service: str, namespace: str = "production") -> dict:
    """查一个服务的 Kubernetes pod 状态。

    Args:
        service:   Deployment 名
        namespace: Kubernetes 命名空间
    """
    return {
        "service": service,
        "replicas": {"desired": 3, "ready": 1, "available": 1},
        "pods": [
            {"name": f"{service}-7d9f8b-xk2p", "phase": "Running", "restarts": 0, "age": "2h"},
            {"name": f"{service}-7d9f8b-mq9r", "phase": "OOMKilled", "restarts": 5, "age": "45m"},
            {"name": f"{service}-7d9f8b-wt4j", "phase": "Pending", "restarts": 0, "age": "1m"},
        ],
    }


@tool(
    meta=ToolMeta(
        name="check_slo",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        estimated_latency_ms=250,
        domain="observability",
    )
)
async def check_slo(service: str) -> dict:
    """检查服务 SLO 是否在燃烧；返回 error budget + burn rate。

    [TRIGGER] 用来判断服务整体健康度、error budget 耗尽情况，或是否需要
    立即升级。
    [MUTEX] 如果需要具体指标值（error_rate、latency_p99、cpu_usage、
    memory_rss），用 query_metrics。check_slo 只返回 budget 和 burn rate
    —— 不是原始时序。
    [CONSTRAINT] 只读；自动返回 1h 和 6h burn rate 窗口（无 window 参数）。

    Args:
        service: Kubernetes 服务名，如 payment-service
    """
    slo = _SLO_BY_SERVICE.get(service, _SLO_DEFAULT)
    return {
        "service": service,
        "error_budget_remaining_pct": slo["error_budget_remaining_pct"],
        "burn_rate_1h": slo["burn_rate_1h"],
        "burn_rate_6h": slo["burn_rate_6h"],
        "alert_fired": slo["burn_rate_1h"] > 1.0,
        "status": "BURNING" if slo["burn_rate_1h"] > 1.0 else "OK",
    }


# 每服务的 SLO 快照。主故障服务（payment-service）燃烧得很猛；
# 下游受害者（checkout-service）退化但没那么严重。不同的值让 agent 把
# 修复范围限定在根因上，而不是把两个服务都当成一样坏。
_SLO_DEFAULT = {"error_budget_remaining_pct": -38.1, "burn_rate_1h": 14.2, "burn_rate_6h": 3.1}
_SLO_BY_SERVICE = {
    "payment-service": {
        "error_budget_remaining_pct": -38.1,
        "burn_rate_1h": 14.2,
        "burn_rate_6h": 3.1,
    },
    "checkout-service": {
        "error_budget_remaining_pct": -5.2,
        "burn_rate_1h": 1.8,
        "burn_rate_6h": 0.4,
    },
}


@tool(
    meta=ToolMeta(
        name="get_recent_deploys",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        estimated_latency_ms=200,
        domain="kubernetes",
    )
)
async def get_recent_deploys(service: str, limit: int = 5) -> dict:
    """列近期部署，用于变更关联。

    Args:
        service: Deployment 名
        limit:   最多返回几条部署
    """
    return {
        "service": service,
        "deploys": [
            {
                "sha": "a3f92b1",
                "tag": "v2.14.1",
                "deployed_at": "2026-06-18T01:47Z",
            },
            {
                "sha": "f8c01d4",
                "tag": "v2.14.0",
                "deployed_at": "2026-06-17T19:30Z",
            },
        ][:limit],
    }


@tool(
    meta=ToolMeta(
        name="get_pr_diff",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        estimated_latency_ms=800,
        domain="git",
    )
)
async def get_pr_diff(sha: str, service: str = "") -> dict:
    """拉一个 commit SHA 的 git diff 和 PR 元数据。

    用来通过审查具体代码变更，把部署和故障关联起来。

    Args:
        sha:     get_recent_deploys 返回的 commit SHA
        service: 可选服务名，作上下文
    """
    _diffs = {
        "a3f92b1": {
            "pr_number": 4412,
            "pr_title": "perf: switch order-batch-processor to per-item allocation for parallelism",
            "merged_at": "2026-06-18T01:44Z",
            "diff": (
                "-    buf := p.bufferPool.Get()  // returns *bytes.Buffer\n"
                "-    defer p.bufferPool.Put(buf)\n"
                "+    bufs := make([]*bytes.Buffer, len(items))  // ← allocates len(items) buffers\n"
                "+    bufs[i] = new(bytes.Buffer)  // ← never returned to pool, never freed\n"
                "+    // BUG: bufs[] allocated per-call, GC cannot collect until ProcessBatch returns"
            ),
            "risk_assessment": "HIGH — removes buffer pool pattern; per-item allocation is O(N) heap growth",
        },
        "f8c01d4": {
            "pr_number": 4399,
            "pr_title": "chore: bump dependencies, update CI config",
            "merged_at": "2026-06-17T19:28Z",
            "diff": "Dependency version bumps only — no logic changes.",
            "risk_assessment": "LOW — dependency updates with no logic changes",
        },
    }
    result = _diffs.get(
        sha,
        {
            "error": f"No diff found for SHA {sha!r}",
            "available_shas": list(_diffs.keys()),
        },
    )
    return {"sha": sha, "service": service, **result}


@tool(
    meta=ToolMeta(
        name="capture_dashboard",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        estimated_latency_ms=2000,
        domain="observability",
    )
)
async def capture_dashboard(service: str, dashboard: str = "service-overview") -> dict:
    """抓取 Grafana dashboard 截图 URL，用于可视化分析。

    生产环境调 Grafana Render API，返回一个签名 S3 URL，
    可以通过 ImageBlock 传给多模态 LLM。

    Args:
        service:   服务名，用于模板化 dashboard
        dashboard: Dashboard slug
    """
    return {
        "service": service,
        "dashboard": dashboard,
        "grafana_url": f"https://grafana.internal/render/d/overview?var-service={service}",
    }
