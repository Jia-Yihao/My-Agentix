"""跨轮**池化**分位数：把所有重复的原始样本并成一个池，再按唯一尺子取分位。

方法学第 4 条：报告用跨轮池化分位数（pooled quantiles），**不是**各轮分位数
再平均——"平均的百分位"对偏态延迟分布是系统性失真（把长尾抹平后报出一个从未
存在过的数字）。本模块因此不接收"每轮一个分位数"的输入形态，只接收原始样本。

分位数本体**委托**全仓唯一实现 ``evals/perf_stats.nearest_rank_quantile``
（2026-10-27 口径统一），本文件不写任何秩算式。守卫：
``tests/test_p145_bench_fingerprint.py``（p135 的 SCAN_ROOTS 不含 bench，由其补位）。
"""

from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional, Sequence

from evals.perf_stats import nearest_rank_quantile


def pooled_summary(samples_ms: Sequence[float]) -> Dict[str, Any]:
    """原始毫秒样本池 → p50/p90/p95/p99/max/mean/count（最近邻秩，全仓一把尺子）。

    空样本 ⇒ 分位数全 ``None``、``count=0``。不返回 0.0：把"没有样本"和
    "样本都很快"混进同一个值是性能门禁最容易的自欺（与 ``evals/perf_stats``
    的 None 语义逐字一致）。
    """
    pooled: List[float] = [float(v) for v in samples_ms]
    if not pooled:
        return {
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "max": None,
            "mean": None,
            "count": 0,
        }
    mean = statistics.fmean(pooled)
    return {
        "p50": _quantile(pooled, 0.50),
        "p90": _quantile(pooled, 0.90),
        "p95": _quantile(pooled, 0.95),
        "p99": _quantile(pooled, 0.99),
        "max": round(max(pooled), 3),
        "mean": round(mean, 3),
        "count": len(pooled),
    }


def _quantile(values: Sequence[float], q: float) -> Optional[float]:
    """分位点取值——委托唯一实现后保留两位小数毫秒的可读形态。"""
    value = nearest_rank_quantile(values, q)
    return None if value is None else round(value, 3)
