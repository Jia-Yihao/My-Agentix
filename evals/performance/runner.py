"""性能基准 runner（离线、进程内假件，零网络/零 LLM/零 Docker）。

配置键口径见 `docs/architecture/配置管理.md` §5。

九类负载（queries.jsonl，覆盖 HTTP 热路径 / SSE 流 / 发射器 CPU 地板）：

===========  ==========================================================
kind=http    TestClient 真实应用栈（lifespan 一次）——liveness / metrics
             / docs / openapi / root / api 六个免依赖端点
kind=stream  检索域 ``_stream_answer_impl`` 全链（假件见 fakes.py），
             含 Run 登记与 W0 TTFT/duration 戳记；progress 变体额外
             覆盖 retrieval_ms 汇总
kind=cpu     RunEventEmitter 单流 1000 帧 + 终帧戳记的纯 CPU 地板
===========  ==========================================================

用法::

    python evals/performance/runner.py                    # 跑基准 → results/last_run.json
    python evals/performance/runner.py --iterations 5
    python evals/performance/runner.py --skip-http        # 只跑进程内两族
    python evals/performance/runner.py --gate             # 跑完直接对 baseline 判门禁
    python evals/performance/perf_gate.py --record --results <file>   # 录制基线

口径纪律：每类每轮取 ``perf_counter`` 墙钟毫秒；报告 p50/p95/max/mean。
基线录制与门禁必须使用**相同 iterations**（门禁文件头会记录并校验）。
分位数一律走 ``evals/perf_stats.nearest_rank_quantile``（全仓唯一实现）；
缺省 iterations 自 2026-10-27（T2）从 3 抬到 5——重复 <5 的样本没有可发布的尾部。
本文件的秩下标同日由 ``sorted[round(p/100*n)-1]`` 换成 ``sorted[round(q*(n-1))]``
⇒ 此前的 ``baseline.json`` 读数与新口径不同尺，判门禁前应重录（本轮按裁决不跑评测）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

PERF_DIR = Path(__file__).resolve().parent
QUERIES_PATH = PERF_DIR / "queries.jsonl"
RESULTS_DIR = PERF_DIR / "results"
DEFAULT_OUT = RESULTS_DIR / "last_run.json"
DEFAULT_BASELINE = PERF_DIR / "baseline.json"

# 直跑（python evals/performance/runner.py）时 sys.path[0]=evals/performance/，
# 需补仓库根才能 import agentix / evals 包；pytest（pythonpath=["."]）下已存在。
_REPO_ROOT = str(PERF_DIR.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# 分位数只有一把尺子：实现住在 evals/perf_stats.py（2026-10-27 口径统一）。
from evals.perf_stats import nearest_rank_quantile  # noqa: E402  （依赖上面的根注入）

# 与 tests/conftest.py 同款兜底：直跑/CI 无 conftest 时 DATABASE_URL 落
# 进程内 SQLite（HTTP 族 lifespan 的 create_all 不触真库）。setdefault：
# 外部显式配置的真库仍优先。
import os  # noqa: E402  （必须在 agentix 导入前生效）

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


# ---------------------------------------------------------------------------
# 查询集与统计
# ---------------------------------------------------------------------------


def load_queries(path: Path = QUERIES_PATH) -> List[Dict[str, Any]]:
    """读取 queries.jsonl（容忍空行与 # 注释行）。"""
    queries: List[Dict[str, Any]] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            queries.append(json.loads(line))
        except json.JSONDecodeError as exc:  # pragma: no cover - 数据文件损坏
            raise ValueError(f"{path}:{line_no} 不是合法 JSON：{exc}") from exc
    if not queries:
        raise ValueError(f"{path} 没有任何查询")
    return queries


def _percentile(samples: Sequence[float], p: float) -> float:
    """最近秩百分位——**委托**全仓唯一实现 ``evals/perf_stats.nearest_rank_quantile``。

    本函数保留自己的两点局部立场：入参是 0–100 的百分数（对外报表口径），
    空样本返回 0.0（沿用本 runner 既有读数形态）。真正的定标算式不再自己写一份：
    2026-10-27 之前本文件用的是 ``sorted[round(p/100*n)-1]``，与 ``evals/perf_stats.py`` 的
    ``sorted[round(q*(n-1))]`` 是**两种秩**，小样本下 p50 会差一个位次 ⇒ 历史读数
    与新读数不可直接比，需要重录基线（``perf_gate.py --record``）。
    """
    value = nearest_rank_quantile(samples, p / 100.0)
    return 0.0 if value is None else value


def summarize(samples: List[float]) -> Dict[str, float]:
    """毫秒样本 → p50/p90/p95/p99/max/mean/qps/count（最近秩分位，升序）。"""
    ordered = sorted(samples)
    mean = statistics.fmean(ordered) if ordered else 0.0
    return {
        "p50": round(_percentile(ordered, 50), 3),
        "p90": round(_percentile(ordered, 90), 3),
        "p95": round(_percentile(ordered, 95), 3),
        "p99": round(_percentile(ordered, 99), 3),
        "max": round(ordered[-1], 3) if ordered else 0.0,
        "mean": round(mean, 3),
        # 顺序压测口径：QPS = 1000/mean（毫秒→每秒完成数），非并发吞吐
        "qps": round(1000.0 / mean, 2) if mean > 0 else 0.0,
        "count": float(len(ordered)),
    }


# ---------------------------------------------------------------------------
# 三族负载执行器
# ---------------------------------------------------------------------------


def _run_http(queries: List[Dict[str, Any]], iterations: int) -> Dict[str, List[float]]:
    """TestClient 全应用栈（lifespan 一次），免依赖端点逐类计时。"""
    from fastapi.testclient import TestClient

    from agentix.main import application

    samples: Dict[str, List[float]] = {q["class"]: [] for q in queries}
    with TestClient(application) as client:
        for q in queries:
            method, path = q.get("method", "GET"), q["path"]
            for _ in range(iterations):
                t0 = time.perf_counter()
                resp = client.request(method, path)
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                if resp.status_code >= 500:
                    raise RuntimeError(
                        f"{method} {path} 返回 {resp.status_code}（基准要求端点可用）"
                    )
                samples[q["class"]].append(elapsed_ms)
    return samples


def _run_stream(queries: List[Dict[str, Any]], iterations: int) -> Dict[str, List[float]]:
    """检索域流全链（假件），每类每轮一个完整 ``_stream_answer_impl``。"""
    from evals.performance.fakes import collect_stream, patched_stream, reset_state

    reset_state()
    samples: Dict[str, List[float]] = {}
    for q in queries:
        class_name = q["class"]
        samples[class_name] = []
        with patched_stream(include_progress=bool(q.get("progress"))):

            async def _batch(cn: str = class_name) -> List[float]:
                batch: List[float] = []
                for _ in range(iterations):
                    t0 = time.perf_counter()
                    frames = await collect_stream()
                    batch.append((time.perf_counter() - t0) * 1000.0)
                    if not frames:
                        raise RuntimeError(f"{cn} 产出 0 帧")
                return batch

            samples[class_name] = asyncio.run(_batch())
    reset_state()
    return samples


def _run_cpu(queries: List[Dict[str, Any]], iterations: int) -> Dict[str, List[float]]:
    """RunEventEmitter 单流 N 帧 + 终帧戳记的纯 CPU 地板。"""
    from agentix.application.services.run_event_stream import RunEventEmitter

    samples: Dict[str, List[float]] = {}
    for q in queries:
        n_frames = int(q.get("frames", 1000))
        class_name = q["class"]
        samples[class_name] = []
        for _ in range(iterations):
            emitter = RunEventEmitter(run_id="run-perf-cpu")
            t0 = time.perf_counter()
            for i in range(n_frames):
                emitter.attach({"type": "progress", "detail": {"step": i}})
            emitter.attach({"type": "finished", "answer": "done"})
            samples[class_name].append((time.perf_counter() - t0) * 1000.0)
    return samples


# ---------------------------------------------------------------------------
# 汇总与落盘
# ---------------------------------------------------------------------------


def run_benchmark(
    iterations: int = 5,
    *,
    queries_path: Path = QUERIES_PATH,
    http: bool = True,
    stream: bool = True,
    cpu: bool = True,
) -> Dict[str, Any]:
    """执行基准并返回结构化结果（不落盘）。"""
    if iterations < 1:
        raise ValueError("iterations 必须 >= 1")
    queries = load_queries(queries_path)

    kinds = set()
    if http:
        kinds.add("http")
    if stream:
        kinds.add("stream")
    if cpu:
        kinds.add("cpu")

    raw: Dict[str, List[float]] = {}
    t_start = time.perf_counter()
    per_kind: Dict[str, bool] = {}
    for kind in ("http", "stream", "cpu"):
        selected = [q for q in queries if q.get("kind") == kind and kind in kinds]
        if not selected:
            continue
        runner = {"http": _run_http, "stream": _run_stream, "cpu": _run_cpu}[kind]
        per_kind[kind] = True
        raw.update(runner(selected, iterations))
    total_s = time.perf_counter() - t_start
    if not raw:
        raise RuntimeError("没有任何负载被执行（--skip 全开？）")

    all_samples: List[float] = [s for v in raw.values() for s in v]
    from agentix.config import settings

    return {
        "meta": {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "iterations": iterations,
            "app_version": settings.APP_VERSION,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "kinds": sorted(per_kind),
            "duration_total_s": round(total_s, 3),
            "protocol": (
                "perf_counter ms/query; p50/p90/p95/p99 nearest-rank"
                "（evals/perf_stats：sorted[round(q*(n-1))]，2026-10-27 起全仓一把尺子）; "
                "qps=1000/mean（顺序）"
            ),
        },
        "queries": [
            {"class": q["class"], "kind": q.get("kind"), **summarize(raw[q["class"]])}
            for q in queries
            if q["class"] in raw
        ],
        "overall": summarize(all_samples),
    }


def write_results(results: Dict[str, Any], out_path: Path = DEFAULT_OUT) -> Path:
    """结果落盘（目录自动创建）。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return out_path


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="性能基准 runner（离线）")
    parser.add_argument(
        "--iterations",
        type=int,
        default=5,
        help="每类每轮次数（默认 5——重复 <5 的分位数没有可发布的尾部，T2 口径）",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="结果 JSON 输出路径")
    parser.add_argument("--queries", type=Path, default=QUERIES_PATH, help="查询集路径")
    parser.add_argument("--skip-http", action="store_true", help="跳过 HTTP 族")
    parser.add_argument("--skip-stream", action="store_true", help="跳过流族")
    parser.add_argument("--skip-cpu", action="store_true", help="跳过 CPU 地板族")
    parser.add_argument("--gate", action="store_true", help="跑完对 baseline 执行门禁")
    args = parser.parse_args(argv)

    results = run_benchmark(
        args.iterations,
        queries_path=args.queries,
        http=not args.skip_http,
        stream=not args.skip_stream,
        cpu=not args.skip_cpu,
    )
    out = write_results(results, args.out)
    print(f"结果已写入 {out}")
    print(f"{'class':24s} {'p50':>9s} {'p95':>9s} {'max':>9s} {'n':>5s}")
    for row in results["queries"]:
        print(
            f"{row['class']:24s} {row['p50']:9.2f} {row['p95']:9.2f} "
            f"{row['max']:9.2f} {int(row['count']):5d}"
        )
    print(
        f"{'OVERALL':24s} {results['overall']['p50']:9.2f} "
        f"{results['overall']['p95']:9.2f} {results['overall']['max']:9.2f} "
        f"{int(results['overall']['count']):5d}"
    )

    if args.gate:
        from evals.performance.perf_gate import run_gate

        return run_gate(results_path=out, baseline_path=DEFAULT_BASELINE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
