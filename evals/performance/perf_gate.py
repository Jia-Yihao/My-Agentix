"""Phase H·W0/W7 性能门禁：基准结果 vs 已录制基线（W7 起为 CI 阻塞门禁）。

口径：逐负载类比较 ``p50``，`now > baseline * ratio`（默认 3.0，容忍共享
CI 抖动与跨机差异）判回归；overall p50 同规则。新类（基线没有）只提示不判失败；
基线缺失则整体跳过（与 eval-offline 同款「绿灯需自证」语义，打印录制指引）。

用法::

    python evals/performance/perf_gate.py --results evals/performance/results/last_run.json
    python evals/performance/perf_gate.py --record --results <file>   # 用结果录制/更新基线

退出码：0 = 通过或跳过；1 = 回归；2 = 用法/文件错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PERF_DIR = Path(__file__).resolve().parent
DEFAULT_BASELINE = PERF_DIR / "baseline.json"
DEFAULT_RESULTS = PERF_DIR / "results" / "last_run.json"

_REPO_ROOT = str(PERF_DIR.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

DEFAULT_RATIO = 3.0


def load_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} 不是合法 JSON：{exc}") from exc


def _index_by_class(results: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {row["class"]: row for row in results.get("queries", [])}


def compare(
    results: Dict[str, Any],
    baseline: Dict[str, Any],
    *,
    ratio: float = DEFAULT_RATIO,
) -> List[str]:
    """返回回归描述列表（空 = 无回归）。"""
    base_by_class = _index_by_class(baseline)
    now_by_class = _index_by_class(results)
    problems: List[str] = []

    for cls, base_row in sorted(base_by_class.items()):
        now_row = now_by_class.get(cls)
        if now_row is None:
            print(f"[skip] {cls}: 本轮结果缺失（不判回归，仅提示）")
            continue
        base_p50, now_p50 = float(base_row["p50"]), float(now_row["p50"])
        limit = base_p50 * ratio
        if now_p50 > limit:
            problems.append(
                f"{cls}: p50 {now_p50:.2f}ms > 基线 {base_p50:.2f}ms × {ratio} = {limit:.2f}ms"
            )
        else:
            print(
                f"[ok]   {cls}: p50 {now_p50:.2f}ms（基线 {base_p50:.2f}ms，"
                f"阈值 {limit:.2f}ms）"
            )

    base_overall = float(baseline.get("overall", {}).get("p50", 0) or 0)
    now_overall = float(results.get("overall", {}).get("p50", 0) or 0)
    if base_overall > 0:
        limit = base_overall * ratio
        if now_overall > limit:
            problems.append(
                f"OVERALL: p50 {now_overall:.2f}ms > 基线 {base_overall:.2f}ms × {ratio}"
            )
        else:
            print(
                f"[ok]   OVERALL: p50 {now_overall:.2f}ms（基线 {base_overall:.2f}ms）"
            )

    new_classes = sorted(set(now_by_class) - set(base_by_class))
    for cls in new_classes:
        print(f"[new]  {cls}: 基线无此类（下一此录制时补入）")
    return problems


def record_baseline(results_path: Path, baseline_path: Path = DEFAULT_BASELINE) -> Path:
    """用当前结果覆盖录制基线（含迭代数/协议头，供门禁校验口径）。"""
    results = load_json(results_path)
    baseline = {
        "meta": {
            "recorded_at": results.get("meta", {}).get("timestamp"),
            "iterations": results.get("meta", {}).get("iterations"),
            "protocol": results.get("meta", {}).get("protocol"),
            "note": "recorded by perf_gate.py --record",
        },
        "queries": results.get("queries", []),
        "overall": results.get("overall", {}),
    }
    baseline_path.write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return baseline_path


def run_gate(
    *,
    results_path: Optional[Path] = None,
    results: Optional[Dict[str, Any]] = None,
    baseline_path: Path = DEFAULT_BASELINE,
    ratio: float = DEFAULT_RATIO,
) -> int:
    """执行门禁判定，返回进程退出码。"""
    if results is None:
        if results_path is None:
            results_path = DEFAULT_RESULTS
        try:
            results = load_json(results_path)
        except FileNotFoundError:
            print(
                f"::warning title=perf-gate::未找到结果文件 {results_path}——"
                "门禁跳过（先跑 runner.py）"
            )
            return 0
        except ValueError as exc:
            print(f"::error title=perf-gate::{exc}")
            return 2

    if not baseline_path.exists():
        print(
            f"::warning title=perf-gate::未提交 {baseline_path}——门禁跳过。"
            "录制：python evals/performance/runner.py && "
            "python evals/performance/perf_gate.py --record"
        )
        return 0

    try:
        baseline = load_json(baseline_path)
    except (ValueError, FileNotFoundError) as exc:
        print(f"::error title=perf-gate::基线文件不可读：{exc}")
        return 2

    base_iters = baseline.get("meta", {}).get("iterations")
    now_iters = results.get("meta", {}).get("iterations")
    if base_iters and now_iters and base_iters != now_iters:
        print(
            f"::warning title=perf-gate::迭代数不一致（基线 {base_iters} vs "
            f"本轮 {now_iters}）——p50 可比性下降，仍按 {ratio}× 判定"
        )

    problems = compare(results, baseline, ratio=ratio)
    if problems:
        for p in problems:
            print(f"::error title=perf-regression::{p}")
        print(f"性能回归 {len(problems)} 项（阈值 {ratio}×基线 p50）")
        return 1
    print("性能门禁通过（无回归）")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Phase H W0 性能门禁")
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS, help="基准结果 JSON")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE, help="基线 JSON")
    parser.add_argument("--ratio", type=float, default=DEFAULT_RATIO, help="回归倍数阈值")
    parser.add_argument("--record", action="store_true", help="用结果录制/更新基线后退出")
    args = parser.parse_args(argv)

    if args.record:
        try:
            out = record_baseline(args.results, args.baseline)
        except FileNotFoundError:
            print(f"::error title=perf-gate::未找到结果文件 {args.results}——先跑 runner")
            return 2
        print(f"基线已录制 → {out}")
        return 0

    return run_gate(results_path=args.results, baseline_path=args.baseline, ratio=args.ratio)


if __name__ == "__main__":
    raise SystemExit(main())
