"""Phase H·W0 性能基准包（PhaseH_高性能运行时优化规划 §4 W0）。

- ``queries.jsonl``：九类离线负载（HTTP / SSE 流 / 发射器 CPU）；
- ``fakes.py``：进程内假件（零 LLM/网络/Docker）；
- ``runner.py``：基准执行与结果落盘（``python evals/performance/runner.py``）；
- ``perf_gate.py``：对 ``baseline.json`` 的 p50 软门禁（CI perf-smoke，continue-on-error）。
"""

from evals.performance.runner import load_queries, run_benchmark, summarize, write_results

__all__ = ["load_queries", "run_benchmark", "summarize", "write_results"]
