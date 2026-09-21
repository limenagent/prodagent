"""Per-step resilience — timeout and retry backoff are replaceable policies
attached to the node.

Transient node failures (network jitter, downstream throttling) are common.
Give a node a RetryPolicy and the scheduler retries it with exponential
backoff; add a timeout, and an attempt that runs over the limit counts as one
failure and enters the same retry. The mechanism lives in the kernel; how
many times to try, how long to wait, and which errors are worth retrying are
all decided by this policy — replaceable as a whole.

Run: PYTHONPATH=. python3 examples/retry_timeout.py
"""

import asyncio

from src import Workflow
from src.kernel import RetryPolicy


async def main():
    attempts = {"n": 0}

    async def flaky_api(_, ctx):
        """Simulates a downstream API that times out / errors twice and
        succeeds on the third call."""
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError(f"attempt {attempts['n']}: downstream temporarily unavailable")
        return "Third attempt succeeded, result in hand"

    wf = Workflow()
    wf.add_node(
        "call",
        flaky_api,
        terminal=True,
        retry=RetryPolicy(max_attempts=4, base_delay=0.05, factor=2, retry_on=(ConnectionError,)),
    )
    wf.entry("call")

    result = await wf.run("Call a flaky API once")
    print("Result:", result.output)
    print(f"attempts actually made: {attempts['n']} (first try + 2 backoff retries)")


if __name__ == "__main__":
    asyncio.run(main())
