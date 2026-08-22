"""FakeLLM 脚本 —— 驱动编排，不驱动智力。

脚本返回吐固定响应队列的 FakeLLM，让测试测框架的接线（工具顺序、
spawn fan-out、JSON 契约）而不用打真 LLM。
"""

from aiops.testing.fake_llm_scripts import oom_happy_path_script

__all__ = [
    "oom_happy_path_script",
]
