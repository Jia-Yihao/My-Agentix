"""bench 统一入口：``python -m bench.run --target <name> [--repeats N] [--out-dir DIR]``

设计立场（T1 卡 + 方法学）：

- 参数化"跑哪个被测对象、重复几次、结果落哪"；默认产物落 **gitignored** 的
  ``bench/results/``，评测原始数据不进 git；
- 重复下限 ``MIN_REPEATS=5``：低于下限用 ``parser.error`` 响亮拒绝（退出码 2），
  不产出文件——悄悄降样本是基准工具最常见的自我欺骗；
- 被测对象需要外部依赖（HTTP 端点等）而环境缺失 ⇒ 显式
  ``skipped_no_environment`` 状态 + 空读数 + 指纹照落。**不为跑通伪造读数**；
- 本骨架不下任何性能结论：``selfcheck`` 的读数逐字标记
  ``skeleton_selfcheck_not_a_measurement``。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# 直跑 ``python -m bench.run`` 时仓库根已在 sys.path（-m 语义）；显式补齐
# 兼容 cwd 不在仓库根的调用方式，与 scripts/evals 的既有注入手法同款。
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from bench import targets as bench_targets
from bench.aggregate import pooled_summary
from bench.fingerprint import MIN_REPEATS, collect_fingerprint
from bench.results import DEFAULT_RESULTS_DIR, write_result


def _families(raw: str) -> Dict[str, bool]:
    names = [item.strip() for item in raw.split(",") if item.strip()]
    allowed = {"http", "stream", "cpu"}
    unknown = [n for n in names if n not in allowed]
    if unknown or not names:
        raise ValueError(f"--families 只接受 {sorted(allowed)} 的逗号子集，收到 {raw!r}")
    return {name: name in names for name in allowed}


def build_payload(argv: Optional[List[str]]) -> Dict[str, Any]:
    """解析参数 → 采集 → 整形出完整结果 payload（不落盘，便于单测复用）。"""
    parser = argparse.ArgumentParser(
        prog="python -m bench.run",
        description="统一基准入口：每次测量强制落环境指纹（T1 骨架）",
    )
    parser.add_argument("--target", required=True, choices=sorted(bench_targets.TARGETS))
    parser.add_argument(
        "--repeats", type=int, default=MIN_REPEATS,
        help=f"重复次数（方法学下限 {MIN_REPEATS}，低于下限直接拒绝）",
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument(
        "--temperature", type=float, default=None,
        help="模型采样温度；与被测模型无关的对象省略即记 not_applicable（触碰模型必传）",
    )
    parser.add_argument("--url", default=None, help="load_test 目标 URL（缺省=无环境，跳过）")
    parser.add_argument("--requests", type=int, default=200, help="load_test 单轮总请求数")
    parser.add_argument("--workers", type=int, default=10, help="load_test 单轮并发线程数")
    parser.add_argument(
        "--families", default="http,stream,cpu",
        help="perf_runner 负载族子集（透传给 run_benchmark 的开关，不改其判定）",
    )
    parser.add_argument(
        "--samples", type=int, default=10,
        help="retrieval_* 类 target 每轮采样数（跨轮并池；其余 target 忽略）",
    )
    parser.add_argument(
        "--index-path", default=None,
        help="retrieval_vectorstore_build 的 FAISS 索引目录；缺省=运行时生成小 fixture",
    )
    args = parser.parse_args(argv)

    if args.repeats < MIN_REPEATS:
        parser.error(
            f"--repeats={args.repeats} 低于方法学下限 {MIN_REPEATS}"
            "（重复 <5 没有可发布的尾部，T2 同一口径）"
        )
    if args.samples < 1:
        parser.error(f"--samples={args.samples} 非法：每轮至少 1 个样本")

    spec = bench_targets.resolve(args.target)
    options: Dict[str, Any] = {
        "url": args.url,
        "requests": args.requests,
        "workers": args.workers,
        "families": _families(args.families),
        "samples": args.samples,
        "index_path": args.index_path,
    }

    fingerprint = collect_fingerprint(
        target=args.target,
        entry=spec["entry"],
        repeats=args.repeats,
        touches_model=bool(spec["touches_model"]),
        temperature=args.temperature,
    )

    payload: Dict[str, Any] = {
        "fingerprint": fingerprint,
        "target": args.target,
        "data_class": (
            bench_targets.SELF_CHECK_DATA_CLASS
            if args.target == "selfcheck"
            else "benchmark_measurement"
        ),
    }

    try:
        outcome = spec["measure"](args.repeats, options)
    except bench_targets.SkipNoEnvironment as exc:
        # 跳过不是失败也不是成功：一份**有指纹、无读数**的结果文件，
        # 让"未实测"在产物层面可被观察到。
        payload["measurement_status"] = "skipped_no_environment"
        payload["skip_reason"] = str(exc)
        payload["results"] = {
            "pooled_quantiles_ms": None,
            "pooled_status": "not_measured_skipped",
            "pooled_note": str(exc),
            "per_repeat": [],
            "notes": None,
        }
        payload["out_dir"] = str(args.out_dir)
        return payload

    form = outcome["form"]
    if form == "raw_samples":
        pooled = pooled_summary(outcome["samples_ms"])
        pooled_status = "pooled_from_raw_samples"
        note = (
            f"{args.repeats} 轮原始样本（共 {pooled['count']} 个）并池后取"
            "最近邻秩——非各轮分位数再平均"
        )
        per_repeat = outcome.get("per_repeat") or []
    elif form == "internal_pooled":
        pooled = outcome["pooled_quantiles_ms"]
        pooled_status = "internal_pooled_by_target"
        note = (
            "池化发生在原接口内部（runner.summarize 对每类 iterations 个原始样本"
            "取最近邻秩），bench 不叠第二层重复，也不得对各轮再平均"
        )
        per_repeat = outcome["per_repeat"]
    elif form == "summary_only":
        pooled = None
        pooled_status = "not_available_target_exposes_summaries_only"
        note = outcome.get("per_repeat_note", "原接口不回吐原始样本，跨轮合并分位禁止")
        per_repeat = outcome["per_repeat"]
    else:  # pragma: no cover - target 登记表写错形态必须炸，不许静默
        raise RuntimeError(f"未知测量返回形态 {form!r}")

    payload["measurement_status"] = "completed"
    payload["skip_reason"] = None
    payload["results"] = {
        "pooled_quantiles_ms": pooled,
        "pooled_status": pooled_status,
        "pooled_note": note,
        "per_repeat": bench_targets.jsonable(per_repeat),
        "quantile_source": "evals.perf_stats.nearest_rank_quantile",
        # 目标自带的测量口径/caveat（如 T12 的跨栈提醒）：随结果一起落盘，
        # 不让"这只是哪一栈的读数"只活在被测者的记忆里。
        "notes": outcome.get("notes"),
    }
    payload["out_dir"] = str(args.out_dir)
    return payload


def main(argv: Optional[List[str]] = None) -> int:
    payload = build_payload(argv)
    parser_out_dir = Path(payload.pop("out_dir"))
    path = write_result(payload, out_dir=parser_out_dir)
    status = payload["measurement_status"]
    print(f"[bench] target={payload['target']} status={status}")
    print(f"[bench] 结果（含指纹块）已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
