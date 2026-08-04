"""AIOps evals —— 两层评测栈（lesson 19）。

  · dataset.py  —— 版本化的 GoldenDataset（红线 + judge 参考）
  · factory.py  —— 为一个 example 构建并跑真实 agent
  · runner.py   —— CLI: 硬门禁 + LLM-as-Judge，基线捕获，回归门禁

运行: ``make evals`` 或 ``python -m evals.runner --smoke``。
"""
