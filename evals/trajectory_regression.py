"""F5 轨迹前缀回归：基线轨迹 vs 候选轨迹的逐位置比对。

设计依据：``My Agentix_架构优化方案.md`` §9.1「双层回归：端到端 + 轨迹前缀」。

**为什么需要它**：评测集（``run_eval.py``）对整题打分（route / citation /
reject），只能回答「**答对了没**」。重构（换模型、改 prompt、调拓扑）之后
即使答案侥幸正确，**走过的路径**也可能已经漂移——这类回归在整题打分下完全
不可见。轨迹前缀回归回答的是「**走的还是不是同一条路**」。

**两层回归的分工**：

================  ==========================  ==========================
层                工具                        回答的问题
================  ==========================  ==========================
端到端（整题）    ``run_eval.py``             答案对不对（route/citation/reject）
轨迹前缀          ``trajectory_regression.py`` 路径变没变（节点/工具/状态序列）
================  ==========================  ==========================

**输入格式**：两侧均为 ``TrajectoryRecorder.replay()`` 落盘的轨迹包 JSON，
形如 ``{"trace_id": "...", "steps": [{"step_index": 0, "kind": "node",
"name": "guardrails", "status": "ok", ...}, ...]}``。目录输入时按文件逐个读入。

**比较语义**：基线被当作**参考前缀**——候选必须**以基线前缀开头**；
候选更长不算回归（下游多走几步是允许的），候选更短或比较区内任一步不一致
即判定为分歧，并报出**首个分歧位置**与两侧签名。

用法::

    python evals/trajectory_regression.py --baseline evals/baselines --candidate out/traces
    python evals/trajectory_regression.py -b base.json -c cand.json --length 5
    python evals/trajectory_regression.py -b base.json -c cand.json --out report.json

    # 把已落盘的 JSONL 轨迹导出为「基线轨迹包」目录（首次建立基线用）
    # ⚠️ 必须导出到子目录（如 evals/baselines/traces/）——load_packages 对目录内
    # 每个 *.json 要求含 steps 字段；与 quality.json 同级会直接 ValueError。
    python evals/trajectory_regression.py --export-from var/trajectories.jsonl \
                                          --export-to evals/baselines/traces

    # 前缀回归（基线在子目录）
    python evals/trajectory_regression.py -b evals/baselines/traces -c out/candidate

退出码：``0`` 全部匹配；``1`` 存在分歧；``2`` 参数/文件错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 允许 `python evals/trajectory_regression.py` 直接运行（sys.path[0] 是 evals/）
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agentix.application.services.trajectory import (  # noqa: E402
    PREFIX_FIELDS,
    compare_trajectory_packages,
)

__all__ = [
    "load_packages",
    "run_regression",
    "format_report",
    "build_parser",
    "main",
]

_EXIT_OK = 0
_EXIT_DIVERGED = 1
_EXIT_ERROR = 2


def _iter_json_files(path: Path) -> List[Path]:
    """把「文件或目录」统一成待读的 JSON 文件列表（稳定排序）。"""
    if path.is_dir():
        return sorted(p for p in path.glob("*.json") if p.is_file())
    return [path]


def load_packages(path: Path) -> Dict[str, Dict[str, Any]]:
    """读入轨迹包，按 ``trace_id`` 建索引（缺失时回退到文件名 stem）。

    :raises FileNotFoundError: 路径不存在。
    :raises ValueError: 某个 JSON 不是对象或缺少 ``steps`` 字段。
    """
    if not path.exists():
        raise FileNotFoundError(f"路径不存在：{path}")

    packages: Dict[str, Dict[str, Any]] = {}
    for file_path in _iter_json_files(path):
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{file_path} 不是合法 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{file_path} 顶层必须是对象（dict）")
        if "steps" not in payload:
            raise ValueError(f"{file_path} 缺少 'steps' 字段")
        key = str(payload.get("trace_id") or file_path.stem)
        packages[key] = payload
    return packages


def run_regression(
    baseline: Dict[str, Dict[str, Any]],
    candidate: Dict[str, Dict[str, Any]],
    *,
    length: Optional[int] = None,
    fields: Sequence[str] = PREFIX_FIELDS,
) -> Dict[str, Any]:
    """对每个基线 trace 做前缀比对，返回完整报告。

    - **缺失候选**：基线有、候选无 → 记为 ``missing_candidate`` 分歧。
    - **多余候选**：候选有、基线无 → 只计数（不判失败，便于增量接入）。
    """
    cases: List[Dict[str, Any]] = []
    matched = 0
    diverged = 0
    missing = 0

    for trace_id in sorted(baseline):
        base_pkg = baseline[trace_id]
        cand_pkg = candidate.get(trace_id)
        if cand_pkg is None:
            missing += 1
            cases.append(
                {
                    "trace_id": trace_id,
                    "match": False,
                    "reason": "missing_candidate",
                    "compared_length": 0,
                    "baseline_length": len(base_pkg.get("steps") or []),
                    "candidate_length": 0,
                    "first_divergence_index": None,
                    "baseline_step": None,
                    "candidate_step": None,
                }
            )
            continue

        outcome = compare_trajectory_packages(
            base_pkg, cand_pkg, length=length, fields=list(fields)
        )
        cases.append({"trace_id": trace_id, **outcome})
        if outcome["match"]:
            matched += 1
        else:
            diverged += 1

    extra = sorted(set(candidate) - set(baseline))

    return {
        "total_baselines": len(baseline),
        "matched": matched,
        "diverged": diverged,
        "missing_candidate": missing,
        "extra_candidates": extra,
        "length_limit": length,
        "fields": list(fields),
        "cases": cases,
        "ok": diverged == 0 and missing == 0,
    }


def format_report(report: Dict[str, Any]) -> str:
    """终端可读报告（中文）。"""
    lines = [
        "轨迹前缀回归：基线 {total} 条 | 匹配 {ok} | 分歧 {bad} | 候选缺失 {miss}".format(
            total=report.get("total_baselines", 0),
            ok=report.get("matched", 0),
            bad=report.get("diverged", 0),
            miss=report.get("missing_candidate", 0),
        )
    ]
    if report.get("length_limit") is not None:
        lines.append(f"比较范围：仅前 {report['length_limit']} 步")
    lines.append(f"比较字段：{', '.join(report.get('fields', []))}")

    for case in report.get("cases", []):
        if case.get("match"):
            continue
        idx = case.get("first_divergence_index")
        lines.append(
            "  ✗ {tid} @step {idx} ({reason})\n"
            "      基线 {base}\n"
            "      候选 {cand}".format(
                tid=case.get("trace_id"),
                idx="-" if idx is None else idx,
                reason=case.get("reason"),
                base=case.get("baseline_step"),
                cand=case.get("candidate_step"),
            )
        )

    extras = report.get("extra_candidates") or []
    if extras:
        lines.append(f"  · 候选独有 trace {len(extras)} 条（不判失败）：{', '.join(extras[:5])}")

    lines.append("结论：" + ("✅ 轨迹前缀一致" if report.get("ok") else "❌ 检测到轨迹前缀分歧"))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trajectory_regression.py",
        description=(
            "F5 轨迹前缀回归：比对基线与候选的步骤序列，定位首个分歧点。\n"
            "基线的轨迹包可由 TrajectoryRecorder.replay() 落盘生成。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # 注意：`required` 在**解析时**无法感知互斥模式，故此处不设 required，
    # 改在 main() 里按「是否为导出模式」手工校验——否则 --export-from 会被
    # 「缺少 -b/-c」挡在门外（实测踩到过）。
    parser.add_argument(
        "-b",
        "--baseline",
        default=None,
        help="基线轨迹包（单个 .json 或目录，目录内每个 .json 为一个 trace）",
    )
    parser.add_argument(
        "-c",
        "--candidate",
        default=None,
        help="候选轨迹包（同上）",
    )
    parser.add_argument(
        "--length",
        type=int,
        default=None,
        help="只比较基线前 N 步（默认全部）；用于已知下游会变动时守住上游拓扑",
    )
    parser.add_argument(
        "--fields",
        default=",".join(PREFIX_FIELDS),
        help=f"参与比较的步骤字段，逗号分隔（默认：{','.join(PREFIX_FIELDS)}）",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="把完整报告另存为 JSON（可选）",
    )
    parser.add_argument(
        "--export-from",
        default=None,
        help="从 JSONL 轨迹文件（TRAJECTORY_PATH）导出轨迹包，用于建立回归基线",
    )
    parser.add_argument(
        "--export-to",
        default=None,
        help="--export-from 的产出目录（每个 trace 一个 .json）",
    )
    return parser


def _export(jsonl_path: str, out_dir: str) -> int:
    """把 JSONL 轨迹导出为轨迹包目录。"""
    _REPO_ROOT = Path(__file__).resolve().parent.parent
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))

    from agentix.application.services.trajectory import JsonlTrajectorySink, export_trajectory_packages

    sink = JsonlTrajectorySink(Path(jsonl_path))
    count = export_trajectory_packages(sink, out_dir)
    print(f"已从 {jsonl_path} 导出 {count} 条轨迹 -> {out_dir}")
    if count == 0:
        print("[提示] 没有导出任何轨迹：文件为空或不存在。")
        return _EXIT_ERROR
    return _EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI 入口。"""
    args = build_parser().parse_args(argv)

    # 导出基线与比较是互斥的两件事，先处理导出
    if args.export_from:
        if not args.export_to:
            print("[错误] --export-from 需要同时指定 --export-to", file=sys.stderr)
            return _EXIT_ERROR
        return _export(args.export_from, args.export_to)

    # 比较模式：-b / -c 必填（因上面取消了 required，此处手工校验）
    if not args.baseline or not args.candidate:
        print(
            "[错误] 比较模式需要同时指定 -b/--baseline 与 -c/--candidate；"
            "若要导出基线请用 --export-from / --export-to",
            file=sys.stderr,
        )
        return _EXIT_ERROR
    fields: Tuple[str, ...] = tuple(
        f.strip() for f in str(args.fields).split(",") if f.strip()
    ) or PREFIX_FIELDS

    try:
        baseline = load_packages(Path(args.baseline))
        candidate = load_packages(Path(args.candidate))
    except (FileNotFoundError, ValueError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return _EXIT_ERROR

    report = run_regression(
        baseline, candidate, length=args.length, fields=fields
    )

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"报告已写入：{out_path}")

    print(format_report(report))
    return _EXIT_OK if report["ok"] else _EXIT_DIVERGED


if __name__ == "__main__":
    sys.exit(main())
