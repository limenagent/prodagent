"""FakeLLM 脚本 —— 驱动编排，不驱动智力。

每个脚本返回一个吐固定响应队列的 FakeLLM，让 eval 测框架的接线
（工具顺序、spawn fan-out、JSON 契约）而不用打真 LLM。
"""

from aiops.testing.fake_llm_scripts import (
    bad_deploy_script,
    crashloop_script,
    hallucination_fence_script,
    metric_anomaly_script,
    oom_happy_path_script,
)

__all__ = [
    "oom_happy_path_script",
    "hallucination_fence_script",
    "crashloop_script",
    "bad_deploy_script",
    "metric_anomaly_script",
]
