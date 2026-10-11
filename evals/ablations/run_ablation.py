"""Ablation 编排器：按 variants.json 逐变体跑在线评估并汇总 Benchmark 表。

用法（在项目根目录）::

    # 查看变体定义 + 开关接线状态
    .venv/Scripts/python.exe evals/ablations/run_ablation.py --list

    # 跑单个变体（在线：调真实 ChatService，需 LM Studio + Docker 中间件）
    .venv/Scripts/python.exe evals/ablations/run_ablation.py --variant G_full

    # 跑全部变体
    .venv/Scripts/python.exe evals/ablations/run_ablation.py

    # 只聚合已有结果（离线，不出数也能出表骨架）
    .venv/Scripts/python.exe evals/ablations/run_ablation.py --aggregate-only

变体以 ``AGENTIX_ABLATION_*`` 环境变量注入子进程 run_eval（--live）；
读取端在 :mod:`agentix.config.ablation`——该模块是「开关是否真的接进管线」的唯一事实源。

契约守卫（重要）
----------------
若某变体把 **未接线** 的开关设成非默认值（``ablation.UNWIRED_FLAGS``；当前为空——
``multi_agent`` 已于 Phase 19 接线），说明代码里没有可切换的实现：它跑出来的数字与
基线逐位相同，却会以「某变体」的名义进表，把「没有实现」伪装成「实验结论
（该能力无增益）」。

因此 ``run_ablation.py`` 默认**拒绝**执行这类变体并以退出码 2 退出，
除非显式传 ``--allow-unwired``（此时会在表头留下醒目警告，说明该行不代表消融结论）。

聚合用 :mod:`evals.ablations.aggregate`，表落 ``evals/ablations/BENCHMARK.md``。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
RESULTS_ROOT = HERE / "results"

# 允许 `python evals/ablations/run_ablation.py` 直接运行（脚本目录入栈，
# 项目根目录需手动补进 sys.path 才能导入 evals.ablations.*）
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

#: 变体 flags → 环境变量（AGENTIX_ABLATION_<FLAG大写>=true/false）
FLAG_ENV_PREFIX = "AGENTIX_ABLATION_"


def _flag_env(flags: Dict[str, object]) -> Dict[str, str]:
    return {
        f"{FLAG_ENV_PREFIX}{str(k).upper()}": str(bool(v)).lower()
        for k, v in (flags or {}).items()
    }


def _flag_spec():
    """延迟导入开关规格，避免 ``--help`` 时拉起 settings 单例。"""
    from agentix.config.ablation import (
        FLAG_DEFAULTS,
        UNWIRED_FLAGS,
        WIRED_FLAGS,
    )

    return FLAG_DEFAULTS, WIRED_FLAGS, UNWIRED_FLAGS


def check_variant(variant: dict) -> Tuple[List[str], List[str]]:
    """校验变体声明的开关是否都能被代码满足。

    Returns
    -------
    Tuple[List[str], List[str]]
        ``(blocking, notes)``。``blocking`` 非空表示该变体不可信；
        ``notes`` 为无害提示（例如声明了信息性标记）。
    """
    defaults, wired, unwired = _flag_spec()
    blocking: List[str] = []
    notes: List[str] = []

    for name, value in (variant.get("flags") or {}).items():
        if name not in defaults:
            # 拼错的开关名会被管线静默忽略 ⇒ 该变体与基线不可区分，同等拦截。
            blocking.append(
                f"{name}={value} -> 未知开关（不在 FLAG_DEFAULTS 中，管线会静默忽略）"
            )
            continue
        if value == defaults[name]:
            continue  # 与现状一致，不构成「要求代码做不到的事」
        if name in unwired:
            blocking.append(f"{name}={value} -> 未接线：{unwired[name]}")
        elif name in wired:
            notes.append(f"{name}={value} (wired: {wired[name]})")
        else:
            blocking.append(f"{name}={value} 既未接线也不在信息性白名单中")

    return blocking, notes


def validate_variants(variants: List[dict], *, allow_unwired: bool) -> List[str]:
    """打印每个变体的接线状态；返回不可执行的变体 id 列表。"""
    defaults, wired, unwired = _flag_spec()
    print("[ablation] 开关接线状态:")
    print(f"[ablation]   wired  : {', '.join(sorted(wired))}")
    print(
        f"[ablation]   unwired: {', '.join(sorted(unwired)) or '(none)'}"
        f"  | defaults: {json.dumps(defaults, ensure_ascii=False)}"
    )

    blocked: List[str] = []
    for variant in variants:
        blocking, notes = check_variant(variant)
        status = "BLOCKED" if blocking else "ok"
        print(f"[ablation]   {status:8s} {variant['id']}")
        for note in notes:
            print(f"[ablation]            note: {note}")
        for reason in blocking:
            print(f"[ablation]            BLOCK: {reason}")
        if blocking and not allow_unwired:
            blocked.append(variant["id"])
    return blocked


def list_variants() -> List[dict]:
    from evals.ablations.aggregate import load_variants

    return load_variants()


def run_variant(variant: dict, *, python: str | None = None) -> int:
    """对单个变体执行 run_eval --live（子进程，注入 flags 环境变量）。"""
    python = python or sys.executable
    env = os.environ.copy()
    env.update(_flag_env(variant.get("flags")))
    env.setdefault("PYTHONPATH", str(PROJECT_ROOT))

    out_dir = RESULTS_ROOT / variant["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        python, str(PROJECT_ROOT / "evals" / "run_eval.py"),
        "--live", "--results-dir", str(out_dir),
    ]
    print(f"[ablation] {variant['id']}: {' '.join(cmd)}")
    print(f"[ablation] flags: {variant.get('flags')}")
    return subprocess.call(cmd, env=env, cwd=str(PROJECT_ROOT))


def write_benchmark_markdown(*, unwired_ack: bool = False) -> Path:
    """聚合 → BENCHMARK.md（含未跑变体占位行）。"""
    from evals.ablations.aggregate import aggregate, render_markdown

    aggregated = aggregate(RESULTS_ROOT)
    header = (
        "# Agent Benchmark（消融实验）\n\n"
        "> 由 `evals/ablations/run_ablation.py --aggregate-only` 生成；"
        "指标口径与 `evals/run_eval.py` 一致（均 ×100 显示为百分数）。\n\n"
    )
    if unwired_ack:
        header += (
            "> ⚠️ **本次运行使用了 `--allow-unwired`**：表中至少有一个变体声明了"
            "代码未接线的开关，其数值与基线在那些维度上不可区分，"
            "**不能作为「该能力无增益」的消融结论**。\n\n"
        )
    text = header + render_markdown(aggregated)
    out = HERE / "BENCHMARK.md"
    out.write_text(text, encoding="utf-8")
    (HERE / "benchmark.json").write_text(
        json.dumps(aggregated, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[ablation] benchmark written: {out}")
    return out


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent 消融实验编排器")
    parser.add_argument("--list", action="store_true", help="列出变体定义与接线状态后退出")
    parser.add_argument("--variant", help="只跑指定 id 的变体")
    parser.add_argument(
        "--aggregate-only", action="store_true", help="不跑评估，只聚合已有结果"
    )
    parser.add_argument(
        "--allow-unwired",
        action="store_true",
        help=(
            "强制执行声明了未接线开关的变体。默认拒绝——这类变体跑出来与基线"
            "逐位相同，进表会被误读为实验结论。"
        ),
    )
    args = parser.parse_args(argv)

    variants = list_variants()

    if args.list:
        for v in variants:
            print(f"{v['id']:14s} {v.get('label', ''):24s} {v.get('description', '')}")
        print()
        validate_variants(variants, allow_unwired=True)
        return 0

    if args.aggregate_only:
        write_benchmark_markdown()
        return 0

    if args.variant:
        selected = [v for v in variants if v["id"] == args.variant]
        if not selected:
            parser.error(f"unknown variant: {args.variant} (see --list)")
    else:
        selected = variants

    blocked = validate_variants(selected, allow_unwired=args.allow_unwired)
    if blocked:
        print()
        print(
            "[ablation] 拒绝执行：以下变体声明了代码未接线的开关，"
            "无对应实现可切换——跑出来会与基线逐位相同，"
            "却会被当作「该能力无增益」的消融结论："
        )
        for vid in blocked:
            print(f"[ablation]   - {vid}")
        print(
            "[ablation] 若要强行跑（结果仅供占位、不得用于结论），"
            "加 --allow-unwired。"
        )
        return 2

    failures = []
    for variant in selected:
        code = run_variant(variant)
        if code != 0:
            failures.append(variant["id"])

    write_benchmark_markdown(unwired_ack=args.allow_unwired)
    if failures:
        print(f"[ablation] FAILED variants: {', '.join(failures)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
