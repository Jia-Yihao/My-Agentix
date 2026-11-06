"""企业级 P2-6.1/6.3：质量/性能门禁阈值检查器。

两个用途（同一套「不低于基线 -X%」语义）：

1. **golden 检索质量门禁**（§6.1 机制侧）：run_eval.py 产出指标 JSON 后，
   与 committed 基线文件比对，低于基线 -2% 即非零退出（CI 阻断）。
2. **性能基线门禁**（§6.3）：perf-baseline workflow 中比对 p95_seconds，
   劣化超 20% 告警。

三种"没真在把关"的状态必须显式区分
-----------------------------------
门禁最危险的失效方式不是「判错」，而是**没启用却看起来是绿的**。本脚本因此把
三种状态打上不同标签，且都写进 ``$GITHUB_STEP_SUMMARY``（CI 里可在 job 页直接看到）：

* ``PASS`` / ``FAIL`` —— 真比对过，结论可信。
* ``NOT-ARMED`` —— 基线文件不存在，或基线里没有该指标键，**且没有显式要求建档**。
  2026-10-26 起这是默认行为并以非零退出：门禁的输入是 committed 生成物
  （基线生成物由 ``evals/write_quality_baseline.py`` 产出），
  输入缺失或被删键的意思是"这个指标现在没人把关"，而不是"这个指标通过了"。
  旧口径（缺输入 ⇒ 静默建档 + 退出 0）会让"删掉基线里的一个键"成为最省事的放宽方式。
* ``NOT-ENFORCED`` —— 同上两种缺失，但调用方**显式**声明了 ``--allow-bootstrap``
  （首跑建档：把当前值写进基线、以 0 退出，语义是"本轮未比对"，不是"通过"）。
  这条路径存在是因为 ``perf-baseline`` 的 nightly 首跑确实没有基线（缓存可能被清），
  让它每次都硬失败是噪声而不是信号——所以那个 workflow 里这一行是显式写着的。
* ``SKIPPED`` —— 连当前值都没有（上游没跑出 summary）。不在本脚本职责内，
  由 workflow 侧负责显式声明。

``--allow-bootstrap`` 与默认硬失败之间没有第三种形态：**基线文件在拒绝路径上不被改写**。
CI 悄悄改动一个 committed 文件，比它不把关更难被发现。

历史口径纠正
------------
本文档此前写着「当前 golden 集仅 22 条……先扩充到 100+ 再把本检查挂进 CI 阻断」。
该结论在 **102 条时代**成立（更早那句「仅 22 条」说的是另一个时代）：当时 golden 是旧题集，本检查也确实挂在
``.github/workflows/ci.yml`` 的 ``eval-offline`` job；真正卡住门禁的不是条数，而是当时
录制预测档未入库 ⇒ 上游 ``run_eval --offline`` 跳过、门禁步骤连带不执行。
**2026-10-26 再更新（2026-10-27 实测）**：该集与 ``eval-offline`` job 已同步下线（``grep -nE "quality_gate|eval-offline" .github/workflows/ci.yml`` → 0 命中），**CI 侧**现役挂载只剩 ``.github/workflows/perf-baseline.yml:99``（nightly、p95 单指标）；仓内另一消费者 ``evals/evolution_run.py:52`` 复用纯函数 ``evaluate_metric``，不属 CI 挂载。

用法：
    python evals/quality_gate.py --baseline-file baselines/quality.json \\
        --metric hit_at_3 --current 0.85 --max-regression 0.02

基线文件格式（JSON）：{"hit_at_3": 0.9, "citation_accuracy": 0.95, "p95_seconds": 0.12}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

#: GitHub Actions 的 step summary 环境变量（本地运行时不存在）
STEP_SUMMARY_ENV = "GITHUB_STEP_SUMMARY"

#: 「门禁未武装」的退出码。**刻意不等于 FAIL 的 1**：1 意思是"指标劣化了"，
#: 而这里什么都没比对——两者混在一个码上，日志里就分不开"质量退步"与"没人把关"。
NOT_ARMED_EXIT_CODE = 2


def _refuse(message: str, summary_lines: list[str]) -> int:
    """门禁未武装：error 注解 + step summary + 非零退出（**不碰基线文件**）。"""
    print(f"::error title=quality-gate::{message}")
    print(f"[quality-gate] NOT-ARMED: {message}")
    _emit_summary(summary_lines)
    return NOT_ARMED_EXIT_CODE


def _emit_summary(lines: list[str]) -> None:
    """把结论写进 GitHub step summary（不存在则静默跳过，本地运行友好）。"""
    target = os.environ.get(STEP_SUMMARY_ENV)
    if not target:
        return
    try:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError:  # pragma: no cover - 文件系统异常不该打断门禁
        pass


def _warn(message: str) -> None:
    """GitHub 警告注解 + 本地可见输出。"""
    print(f"::warning title=quality-gate::{message}")
    print(f"[quality-gate] NOT-ENFORCED: {message}")


def evaluate_metric(
    current: float,
    baseline_value: float,
    *,
    max_regression: float = 0.02,
    direction: str = "higher_is_better",
) -> dict:
    """「不低于基线 -X%」判定的纯函数版（CLI 与进化回归门共用同一真源）。

    Phase 20 对齐：``evals/evolution_run.py`` 的 ``citation`` 门直接调用本函数，
    与 CI 侧 ``main()`` 用的是同一套阈值语义——不存在"两处各算一遍、口径漂移"。

    :return: ``{"ok", "threshold", "comparison", "verdict"}``。
    """
    if direction == "lower_is_better":
        threshold = baseline_value * (1.0 + max_regression)
        ok = current <= threshold
        comparison = "≤"
    else:
        threshold = baseline_value * (1.0 - max_regression)
        ok = current >= threshold
        comparison = "≥"
    return {
        "ok": ok,
        "threshold": threshold,
        "comparison": comparison,
        "verdict": "PASS" if ok else "FAIL",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline-file", required=True, help="基线 JSON 路径")
    parser.add_argument("--metric", required=True, help="要比对的指标键")
    parser.add_argument("--current", required=True, type=float, help="当前值")
    parser.add_argument(
        "--max-regression",
        type=float,
        default=0.02,
        help="允许的最大相对劣化（0.02 = 基线的 2%%）；p95 类指标建议 0.20",
    )
    parser.add_argument(
        "--direction",
        choices=("higher_is_better", "lower_is_better"),
        default="higher_is_better",
        help="指标方向：hit@3 越高越好 / p95 越低越好",
    )
    parser.add_argument(
        "--allow-bootstrap",
        action="store_true",
        help="显式允许「首跑建档」：缺基线文件/缺指标键时写入当前值并以 0 退出。"
        "默认不开——缺输入现在是硬失败（见模块说明的 NOT-ARMED）",
    )
    args = parser.parse_args()

    baseline_path = Path(args.baseline_file)
    if not baseline_path.exists():
        if not args.allow_bootstrap:
            return _refuse(
                f"基线文件不存在：{baseline_path} ⇒ 指标 {args.metric} 未被把关，"
                "拒绝放行（要首跑建档请显式加 --allow-bootstrap）",
                [
                    "### ❌ quality-gate 未武装（NOT-ARMED）",
                    "",
                    f"- 指标：`{args.metric}`",
                    f"- 原因：基线文件 `{baseline_path}` 不存在",
                    "- 本步骤**没有比对任何值**，job 红是因为门禁缺输入，不是因为指标劣化",
                    f"- 若这是首次建档：用 `evals/write_quality_baseline.py` 从真实 summary 生成，"
                    "或显式加 `--allow-bootstrap`",
                ],
            )
        _warn(
            f"基线文件不存在：{baseline_path}。本次只建档不阻断——"
            f"也就是说本指标**未被把关**，job 变绿不代表质量达标。"
        )
        _emit_summary(
            [
                "### ⚠️ quality-gate 未生效（NOT-ENFORCED）",
                "",
                f"- 指标：`{args.metric}`",
                f"- 原因：基线文件 `{baseline_path}` 不存在",
                f"- 建议写入：`{{\"{args.metric}\": {args.current}}}`",
                "",
                "> 建档完成后重新运行，本指标才会真正参与阻断。",
            ]
        )
        return 0

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    if args.metric not in baseline:
        # 建档语义 ≠ 通过语义。空基线（如 committed 的 `{}`）会让每个指标都走到
        # 这里，于是门禁"每跑必绿"，而日志里看不出它其实什么都没拦。
        if not args.allow_bootstrap:
            # 关键差别：这条路径**不写文件**。CI 改写一个 committed 的基线文件，
            # 等于让"删掉一个键"成为最省事的放宽方式——改完还自动补回来了。
            return _refuse(
                f"基线 {baseline_path} 中没有指标 {args.metric} ⇒ 该键未被把关，"
                "拒绝放行且不改动基线",
                [
                    "### ❌ quality-gate 未武装（NOT-ARMED）",
                    "",
                    f"- 指标：`{args.metric}`",
                    f"- 原因：基线文件 `{baseline_path}` 缺这个键",
                    "- 基线是 `evals/write_quality_baseline.py` 的生成物："
                    "缺键意味着要么该键从未归档（0.0 闸门跳过，属已知形态），"
                    "要么有人删了键。后者必须走重录，不该由 CI 补写",
                ],
            )
        _warn(
            f"基线中没有指标 {args.metric}，已写入当前值建档、本次不阻断——"
            f"该指标从下次运行起才真正参与比对。"
        )
        baseline[args.metric] = args.current
        baseline_path.write_text(
            json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _emit_summary(
            [
                "### ⚠️ quality-gate 首次建档（NOT-ENFORCED）",
                "",
                f"- 指标：`{args.metric}`",
                f"- 基线文件原本没有该键，已写入 `{args.current}`",
                "- 本次未做任何比对，**下次运行起才生效**",
            ]
        )
        return 0

    base_value = float(baseline[args.metric])
    current = float(args.current)
    result = evaluate_metric(
        current,
        base_value,
        max_regression=args.max_regression,
        direction=args.direction,
    )
    ok = bool(result["ok"])
    threshold = float(result["threshold"])
    comparison = str(result["comparison"])
    verdict = str(result["verdict"])
    print(
        f"[quality-gate] {verdict}: {args.metric} current={current:.4f} "
        f"baseline={base_value:.4f} threshold={threshold:.4f} "
        f"({comparison}, max_regression={args.max_regression:.0%})"
    )
    _emit_summary(
        [
            f"### {'✅' if ok else '❌'} quality-gate {verdict}: `{args.metric}`",
            "",
            f"| 当前值 | 基线值 | 阈值 | 关系 | 允许劣化 |",
            f"|---:|---:|---:|:--:|---:|",
            f"| {current:.4f} | {base_value:.4f} | {threshold:.4f} | {comparison} | "
            f"{args.max_regression:.0%} |",
        ]
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
