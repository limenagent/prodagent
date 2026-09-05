"""10 步骤级弹性 —— 超时与重试退避是挂在节点上的可替换策略。

节点偶发失败（网络抖动、下游限流）很常见。给节点一个 RetryPolicy，调度器就会
按指数退避重试；再给一个 timeout，单次执行超过时长就算一次失败、同样进入重试。
机制在内核，“试几次、等多久、哪些错值得重试”都由这份策略决定，可整体替换。

跑法：PYTHONPATH=. python3 examples/10_retry_timeout.py
"""

import asyncio

from src import Workflow
from src.kernel import RetryPolicy


async def main():
    attempts = {"n": 0}

    async def flaky_api(_, ctx):
        """模拟一个前两次超时/报错、第三次才成功的下游接口。"""
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError(f"第 {attempts['n']} 次：下游暂时不可用")
        return "第三次调用成功，拿到结果"

    wf = Workflow()
    wf.add(
        "call",
        flaky_api,
        terminal=True,
        retry=RetryPolicy(max_attempts=4, base_delay=0.05, factor=2, retry_on=(ConnectionError,)),
    )
    wf.entry("call")

    result = await wf.run("请求一次不稳定的接口")
    print("结果：", result.output)
    print(f"实际尝试 {attempts['n']} 次（首次 + 2 次退避重试）")


if __name__ == "__main__":
    asyncio.run(main())
