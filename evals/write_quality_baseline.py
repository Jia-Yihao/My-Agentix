"""从 run_eval 的 summary JSON **生成**质量基线（而不是手抄数字）。

为什么要有这个脚本：质量基线文件是 CI 门禁的核对基准，
基准一旦是手抄的就会漂——工程约定 7 记过两次「矩阵写着 true、override 里
压根没设」这类案发，同一形态在指标基线上同样成立（README 里发布一个数、基线文件
里另一个数，CI 却拿后者判 PASS）。本脚本让基线只有一个来源：某次真实
``run_eval`` 产出的 summary。

三条不讨喜但必要的规则：

1. **0.0 的键不写——分母为 0 或值为 0 都算**。``*_applicable`` 口径下「本轮没有
   适用样本」时 summary 给 ``0.0``；而**分母不为 0、值却是 0.0** 也照样危险
   （录制中断时可评测样本全落在缺预测区间，2026-10-25 实测：分母 34 的
   ``citation_correctness_applicable`` 打出 0.0）。写进去就等于宣布「这个指标
   永远 PASS」（0.0 ≥ 0.0×0.98 恒真），门禁对这个键只剩装饰作用。
   宁可让门禁对该键走 NOT-ENFORCED（响亮地没把关），也不要一个假绿。
2. **缺预测不为零时拒绝建档**。录制中断（``missing_prediction_count > 0``）时
   打分把缺预测计为失败，基线会被系统性压低；此后任何真实劣化都藏在噪声里。
   要覆盖请显式 ``--force``。
3. **写入的是生成物 + 溯源**。``_meta`` 记下来源 summary、金标文件、分母与
   题数，使「这条基线是哪次录制出来的」可核对；门禁按指标名取值，多余键忽略，
   不影响比对。

用法::

    python evals/write_quality_baseline.py \\
        --summary evals/results/<stamp>.summary.json \\
        --baseline-file out/quality.json

    # 只看不写
    python evals/write_quality_baseline.py --summary ... --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

EVALS_DIR: Path = Path(__file__).resolve().parent
REPO_ROOT: Path = EVALS_DIR.parent
DEFAULT_BASELINE: Path = EVALS_DIR / "baselines" / "quality.json"


def _repo_relative(value: object) -> str:
    """把路径写成仓库相对路径（正斜杠）再进 ``_meta``。

    committed 的基线里留一个 ``D:\\\\Code\\\\...`` 绝对路径，等于把「哪台机器录的」
    当成口径的一部分——换机位重录、CI 上读、`git diff` 审查都会被这行噪声带偏。
    """
    text = str(value)
    if not text:
        return text
    try:
        path = Path(text).resolve()
        if path.is_relative_to(REPO_ROOT.resolve()):
            return path.relative_to(REPO_ROOT.resolve()).as_posix()
    except (OSError, ValueError):  # noqa: BLE001 - 解析不了就照原样留痕，不改写事实
        pass
    return text

#: CI ``eval-offline`` 门禁比对的五个键（真源：``.github/workflows/ci.yml`` 的
#: METRICS 元组；``tests/test_ci_gate_contract.py`` 守住两边不脱钩）。
CI_GATE_METRICS: Tuple[str, ...] = (
    "route_accuracy",
    "citation_hit_rate_applicable",
    "citation_correctness_applicable",
    "must_contain_applicable",
    "reject_accuracy_applicable",
)

#: 每个指标的**真实分母**键。没有这一项就无法区分「0.0 分」与「本轮无适用样本」。
DENOMINATOR_BY_METRIC: Dict[str, str] = {
    "route_accuracy": "total",
    "citation_hit_rate_applicable": "citation_expected_count",
    "citation_correctness_applicable": "citation_correctness_expected_count",
    "must_contain_applicable": "must_contain_expected_count",
    "reject_accuracy_applicable": "reject_expected_count",
}


def build_baseline(
    summary: Dict[str, Any],
    *,
    metrics: Tuple[str, ...] = CI_GATE_METRICS,
) -> Tuple[Dict[str, Any], List[str]]:
    """由 summary 构造基线字典，返回 ``(baseline, skipped_notes)``。

    纯函数：不读文件、不写文件、不碰环境，便于测试直接喂构造的 summary。
    """
    baseline: Dict[str, Any] = {}
    skipped: List[str] = []
    for name in metrics:
        if name not in summary:
            raise KeyError(
                f"summary 里没有指标键 {name!r}——run_eval 的产出与门禁口径已脱钩，"
                "请同步本脚本与 ci.yml（不要静默跳过，那等于放弃这个指标）。"
            )
        denom_key = DENOMINATOR_BY_METRIC.get(name)
        denom = summary.get(denom_key) if denom_key else None
        if denom is not None and int(denom) == 0:
            skipped.append(
                f"{name}: 分母 {denom_key}=0（本轮无适用样本），不写基线——"
                "否则该指标恒 PASS"
            )
            continue
        value = summary[name]
        if value is None:
            skipped.append(f"{name}: summary 值为 None（不可评估），不写基线")
            continue
        rounded = round(float(value), 4)
        # 判定用**真正写进文件的那个数**，不是原始浮点：0.00004 会被 round 成 0.0，
        # 而恒 PASS 的形态是由落盘值决定的（阈值 = 0.0×0.98 = 0.0，任何值都 ≥ 它）。
        if rounded == 0.0:
            skipped.append(
                f"{name}: 落盘值为 0.0（原始 {value!r}，分母 {denom_key}={denom}），不写基线——"
                "0.0 基线恒 PASS，归档它等于给这个指标装装饰"
            )
            continue
        baseline[name] = rounded
    return baseline, skipped


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--summary", required=True, help="run_eval 产出的 *.summary.json")
    parser.add_argument(
        "--baseline-file", default=str(DEFAULT_BASELINE), help=f"默认：{DEFAULT_BASELINE}"
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印将写入的内容")
    parser.add_argument(
        "--force",
        action="store_true",
        help="允许缺预测不为零的 summary 建档（默认拒绝，见模块说明规则 2）",
    )
    args = parser.parse_args(argv)

    summary_path = Path(args.summary)
    if not summary_path.exists():
        print(f"[基线] 找不到 summary：{summary_path}", file=sys.stderr)
        return 2
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    summary = payload.get("summary") if isinstance(payload, dict) else None
    if not isinstance(summary, dict):
        print(
            f"[基线] {summary_path} 顶层没有 summary 对象——这不是 run_eval 的汇总产物。",
            file=sys.stderr,
        )
        return 2

    missing = int(summary.get("missing_prediction_count") or 0)
    if missing and not args.force:
        print(
            f"[基线] 该次打分有 {missing} 条缺预测（录制未跑完？）。缺预测按失败计，"
            "基线会被系统性压低——先补录或换一份完整的 summary，"
            "确要覆盖请加 --force。",
            file=sys.stderr,
        )
        return 2

    try:
        baseline, skipped = build_baseline(summary)
    except KeyError as exc:
        print(f"[基线] {exc}", file=sys.stderr)
        return 2

    for note in skipped:
        print(f"[基线] 跳过 {note}")
    if not baseline:
        print(
            "[基线] 五个指标全部被跳过——这份 summary 不足以建档，"
            "quality.json 保持原样（门禁将继续 NOT-ENFORCED）。",
            file=sys.stderr,
        )
        return 2

    baseline["_meta"] = {
        "generated_by": "evals/write_quality_baseline.py",
        "source_summary": _repo_relative(summary_path),
        # 默认题集已随评测集下线删除：summary 没记 golden 字段时**不猜路径**——
        # 在这里留一个指向已删文件的常量，等于在生成物里写死一个死引用（约定 7 同一个毛病）。
        "golden": (
            _repo_relative(summary["golden"])
            if summary.get("golden")
            else "unrecorded（source summary 未记 golden 字段；默认题集已下线，不猜路径）"
        ),
        "total": summary.get("total"),
        "missing_prediction_count": missing,
        "denominators": {
            metric: summary.get(DENOMINATOR_BY_METRIC.get(metric)) or 0
            for metric in baseline
            if DENOMINATOR_BY_METRIC.get(metric)
        },
        "skipped_metrics": skipped,
    }

    out_path = Path(args.baseline_file)
    text = json.dumps(baseline, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.dry_run:
        print(f"[基线] --dry-run，未写入 {out_path}：\n{text}")
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    print(
        f"[基线] 已写入 {out_path}（{len(baseline) - 1} 个指标键 + _meta；"
        f"跳过 {len(skipped)} 个）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
