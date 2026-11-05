"""全仓**唯一**的分位数实现：最近邻秩（nearest-rank）。

**为什么只有一个源**：本仓曾有四种各自实现的"分位数"——最近邻秩的
``sorted[round(q*(n-1))]``（``scripts/demo/report_eval.py``、
``scripts/demo/local_kb_demo.py``）、另一套下标约定的 ``sorted[round(p/100*n)-1]``
（``evals/performance/runner.py``）、线性插值（``scripts/load_test.py``），
以及 ``scripts/eval/mcq_report.py`` 里 p50 用 ``statistics.median`` / p90 用秩的混合。
同一份延迟样本换个入口就换个数字，"性能劣化 20% 就阻断"这类判定于是取决于
调用的是哪个脚本。同一个判定不许两个源（AGENTS.md 硬性约定 6 ④）。

**口径切换日期：2026-10-27（规划文档 T2）。**
自该日起本仓所有 p50/p90/p95/p99 一律是本函数的最近邻秩。⚠️ **历史读数不可直接比**：

- 与线性插值口径的历史数字（如 ``perf-baseline.yml`` 的上一版基线）跨口径，
  做劣化判定前必须先按新口径**重跑基线**；
- ``evals/performance/baseline.json`` 是旧下标约定录的，切换后首跑可能报出
  微小位移（n 较大时位移可忽略，n 很小则 p50 会差一个位次）；重录命令：
  ``.venv/Scripts/python.exe evals/performance/perf_gate.py --record``。
  本轮**不重录**（规划文档 2026-10-27 裁决：不跑评测）。

**尺子只有一把，取值域写死在这里**：``q`` 是 **0–1 的比例**，不是 0–100 的百分数。
调用方按百分数持有（如 ``--p95``）时自己除以 100，别指望这里替你猜——0.95 与 95
传错是本类工具最典型的静默错误。
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

__all__ = ["nearest_rank_quantile", "QUANTILE_CONVENTION", "QUANTILE_SWITCH_DATE"]

#: 口径标识（写进报告/指纹，让读数自带"用哪把尺子量的"）
QUANTILE_CONVENTION = "nearest-rank sorted[round(q*(n-1))]"

#: 口径切换日期：此前的分位数读数与本函数不同尺，不可直接比较。
QUANTILE_SWITCH_DATE = "2026-10-27"


def nearest_rank_quantile(
    values: Sequence[float], q: float
) -> Optional[float]:
    """最近邻秩分位数：``sorted(values)[round(q * (n - 1))]``。

    与 ``README.md`` §15.2 已对外的口径逐字一致（n=23 时 P95 = 第 22 小），
    ``round`` 用 Python 默认的"四舍六入五成双"，不额外修正——改了就和历史
    那批按此式发布的数字对不上，而那正是本模块要延续的唯一尺子。

    :param values: 样本（无序亦可；元素必须可比较、可转 float）
    :param q: 分位点，**0–1 的比例**（0.5 = 中位数，0.95 = P95）
    :return: 分位数；``values`` 为空时返回 ``None``——**不是 0.0**。
        "没有样本"和"样本都很快"是两件事，把它们混在一个返回值里是性能门禁
        最容易自欺的地方（``scripts/load_test.py`` 的原注释就是这个判断）。
        需要 0.0 兜底的调用方自己显式映射，别改这里。
    :raises ValueError: ``q`` 落在 0–1 之外（含把百分数当比例传进来的误用）
    """
    if not 0.0 <= q <= 1.0:
        raise ValueError(
            f"q 必须是 0–1 的比例，收到 {q!r}——百分数（如 95）请先除以 100。"
        )
    ordered: Sequence[Any] = sorted(values)
    if not ordered:
        return None
    return float(ordered[round(q * (len(ordered) - 1))])
