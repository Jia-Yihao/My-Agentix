"""离线进化 runner（Phase 20 规划 §8.3）：轨迹聚合 → 提案 → 回归门 → 灰度。

用法::

    python evals/evolution_run.py --proposal proposals/prompt_v2.json \\
        [--signals signals.jsonl] \\
        [--prefix-baseline PATH --prefix-candidate PATH] \\
        [--quality-baseline quality.json --quality-current current.json] \\
        [--canary-pct 10] [--max-regression 0.02] \\
        [--proposal-log data/evolution/proposals.jsonl]

提案 JSON 形状::

    {"carrier": "prompt", "summary": "...", "payload": {...},
     "touched_paths": ["agentix/..."]}

门禁（**接 Phase 17 门**，与 evals 同名检查对齐）：

- ``golden_prefix`` → ``evals.trajectory_regression.run_regression``
  （真机前缀基线 vs 候选轨迹包）；
- ``citation`` → ``evals.quality_gate.evaluate_metric``
  （与 CI 同一阈值语义，单真源）；
- 输入缺失 → ``not_enforced``，**按未通过处理（fail-closed）**——
  与 CI 侧"首跑建档不阻断"不同：晋升必须有真基线，拒绝伪造（Phase 17 纪律）；
- 其余门名 → ``not_wired``（fail-closed，待各能力自查函数接入）。

三道防线由 ``OfflineEvolutionLoop`` 强制（证据≠指令 / staging 隔离 /
verifier 逻辑保护）——本 runner 只编排，不绕过。

退出码：``0`` 晋升；``1`` 拒绝/门未过；``2`` 输入错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agentix.agent.evolution import (  # noqa: E402
    EvolutionCarrier,
    EvolutionProposal,
    OfflineEvolutionLoop,
    OnlineLoopRecorder,
    jsonl_proposal_sink,
)
from evals.quality_gate import evaluate_metric  # noqa: E402
from evals.trajectory_regression import load_packages, run_regression  # noqa: E402

__all__ = [
    "aggregate_signals",
    "check_citation",
    "check_golden_prefix",
    "evaluate_gates",
    "load_proposal",
    "run",
    "main",
]

_EXIT_OK = 0
_EXIT_REJECTED = 1
_EXIT_INPUT_ERROR = 2

_SIGNAL_FIELDS = ("trace_id", "success", "first_error_stage", "cost_usd", "tags")


def aggregate_signals(path: Optional[str]) -> Optional[Dict[str, Any]]:
    """回放 signals JSONL 进 :class:`OnlineLoopRecorder` 并聚合（三漏斗同源）。

    :return: ``recorder.aggregate()``；未提供/文件不存在 → ``None``（如实报告）。
    """
    if not path:
        return None
    file_path = Path(path)
    if not file_path.exists():
        return None
    recorder = OnlineLoopRecorder()
    for line in file_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # 坏行跳过（聚合是观测，不是门禁）
        if not isinstance(entry, dict):
            continue
        kwargs = {k: entry[k] for k in _SIGNAL_FIELDS if k in entry}
        if "trace_id" not in kwargs or "success" not in kwargs:
            continue  # 缺必填字段的坏行跳过（聚合是观测，不是门禁）
        try:
            recorder.record(**kwargs)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue  # 字段类型不合（如 cost_usd 非数）→ 同样按坏行处理，不崩 runner
    return recorder.aggregate()


def load_proposal(path: str) -> Tuple[EvolutionProposal, List[str]]:
    """读提案 JSON → (EvolutionProposal, touched_paths)。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("提案 JSON 顶层必须是对象")
    carrier = EvolutionCarrier(str(data.get("carrier", "")).strip())
    proposal = EvolutionProposal(
        carrier=carrier,
        summary=str(data.get("summary", "")),
        payload=dict(data.get("payload") or {}),
    )
    touched = [str(p) for p in (data.get("touched_paths") or [])]
    return proposal, touched


def check_golden_prefix(args: argparse.Namespace) -> Dict[str, Any]:
    """golden_prefix 门：真机前缀基线 vs 候选（Phase 17）。"""
    if not args.prefix_baseline or not args.prefix_candidate:
        return {
            "status": "not_enforced",
            "detail": "缺 --prefix-baseline/--prefix-candidate（Phase 17 真机基线）；拒绝伪造，按未通过处理",
        }
    try:
        baseline = load_packages(Path(args.prefix_baseline))
        candidate = load_packages(Path(args.prefix_candidate))
    except (OSError, ValueError) as exc:
        return {"status": "not_enforced", "detail": f"基线/候选读取失败：{exc}"}
    if not baseline:
        # 空基线会让 run_regression 空真通过（diverged==0 and missing==0）
        # ——晋升必须有真基线，空目录按未通过处理（Phase 17 纪律）
        return {"status": "not_enforced", "detail": "基线为空（无轨迹包）；拒绝空基线放行"}
    report = run_regression(baseline, candidate)
    return {
        "status": "pass" if report["ok"] else "fail",
        "detail": (
            f"matched={report['matched']} diverged={report['diverged']} "
            f"missing_candidate={report['missing_candidate']}"
        ),
        "counts": {
            "total_baselines": report["total_baselines"],
            "matched": report["matched"],
            "diverged": report["diverged"],
            "missing_candidate": report["missing_candidate"],
        },
    }


def check_citation(args: argparse.Namespace) -> Dict[str, Any]:
    """citation 门：citation_accuracy 相对基线（与 CI 同一 evaluate_metric 真源）。"""
    if not args.quality_baseline or not args.quality_current:
        return {
            "status": "not_enforced",
            "detail": "缺 --quality-baseline/--quality-current；按未通过处理",
        }
    try:
        baseline = json.loads(Path(args.quality_baseline).read_text(encoding="utf-8"))
        current = json.loads(Path(args.quality_current).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "not_enforced", "detail": f"指标文件读取失败：{exc}"}
    if "citation_accuracy" not in baseline:
        return {
            "status": "not_enforced",
            "detail": "基线无 citation_accuracy（建档语义 ≠ 晋升语义——必须有真基线）",
        }
    if "citation_accuracy" not in current:
        return {"status": "fail", "detail": "当前指标缺 citation_accuracy"}
    try:
        baseline_value = float(baseline["citation_accuracy"])
    except (TypeError, ValueError):
        return {"status": "not_enforced", "detail": "基线 citation_accuracy 非数值"}
    try:
        current_value = float(current["citation_accuracy"])
    except (TypeError, ValueError):
        return {"status": "fail", "detail": "当前 citation_accuracy 非数值（无法证明未劣化）"}
    result = evaluate_metric(
        current_value,
        baseline_value,
        max_regression=args.max_regression,
        direction="higher_is_better",
    )
    return {
        "status": "pass" if result["ok"] else "fail",
        "detail": (
            f"current={current_value:.4f} "
            f"baseline={baseline_value:.4f} "
            f"threshold={float(result['threshold']):.4f} {result['verdict']}"
        ),
    }


#: 已接入的同名检查（与 OfflineEvolutionLoop.regression_gate 对齐；见 test 守卫）
GATE_CHECKERS = {
    "golden_prefix": check_golden_prefix,
    "citation": check_citation,
}


def evaluate_gates(
    gate_names: Sequence[str], args: argparse.Namespace
) -> Dict[str, Dict[str, Any]]:
    """逐门求值；未接入的门名 → ``not_wired``（fail-closed）。"""
    results: Dict[str, Dict[str, Any]] = {}
    for name in gate_names:
        checker = GATE_CHECKERS.get(name)
        if checker is None:
            results[name] = {
                "status": "not_wired",
                "detail": "该门尚无接入检查函数（fail-closed，不视为通过）",
            }
        else:
            results[name] = checker(args)
    return results


def run(args: argparse.Namespace) -> int:
    """完整流水线：聚合 → 提案 → 回归门 → 灰度；报告打到 stdout（JSON）。"""
    report: Dict[str, Any] = {"signals": aggregate_signals(args.signals)}

    try:
        proposal, touched = load_proposal(args.proposal)
    except (OSError, json.JSONDecodeError, ValueError, KeyError) as exc:
        print(json.dumps({"error": f"提案读取失败：{exc}"}, ensure_ascii=False), file=sys.stderr)
        return _EXIT_INPUT_ERROR

    # Phase 30：可选提案档案（append-only 三状态全记；默认空 = 不落盘，
    # stdout 与基线逐键一致）。路径非法（如 evals/baselines/）→ 输入错误 fail-closed。
    log_path = str(getattr(args, "proposal_log", "") or "").strip()
    sink = None
    if log_path:
        try:
            sink = jsonl_proposal_sink(log_path)
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return _EXIT_INPUT_ERROR
        report["proposal_log"] = log_path

    loop = OfflineEvolutionLoop(proposal_sink=sink)
    staged = loop.propose(proposal, touched_paths=touched)
    report["proposal_id"] = staged.proposal_id
    report["carrier"] = proposal.carrier.value
    report["staged"] = staged.staged
    if not staged.staged:
        report["rejected_reason"] = staged.rejected_reason
        report["promoted"] = False
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return _EXIT_REJECTED

    gate_names = loop.regression_gate[proposal.carrier]
    gates = evaluate_gates(gate_names, args)
    report["gates"] = gates
    all_pass = bool(gate_names) and all(g["status"] == "pass" for g in gates.values())
    loop.run_regression(staged.proposal_id, passed=all_pass)
    if not all_pass:
        report["promoted"] = False
        report["rejected_reason"] = "regression_failed"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return _EXIT_REJECTED

    promoted = loop.promote(staged.proposal_id, canary_pct=args.canary_pct)
    report["promoted"] = True
    report["canary_pct"] = float(promoted.payload.get("canary_pct", args.canary_pct))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return _EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--proposal", required=True, help="提案 JSON 路径")
    parser.add_argument("--signals", default="", help="在线信号 JSONL（OnlineLoopRecorder 导出）")
    parser.add_argument(
        "--prefix-baseline", default="", help="golden_prefix 真机基线（文件或目录）"
    )
    parser.add_argument(
        "--prefix-candidate", default="", help="golden_prefix 候选轨迹包（文件或目录）"
    )
    parser.add_argument("--quality-baseline", default="", help="quality.json 基线路径")
    parser.add_argument("--quality-current", default="", help="当前指标 JSON 路径")
    parser.add_argument(
        "--max-regression",
        type=float,
        default=0.02,
        help="citation 门允许的最大相对劣化（默认 0.02 = 基线的 2%%）",
    )
    parser.add_argument(
        "--canary-pct", type=float, default=10.0, help="晋升灰度比例（默认 10%%）"
    )
    parser.add_argument(
        "--proposal-log",
        default="",
        help=(
            "提案三状态 append-only 档案（JSONL）；默认空 = 不落盘（stdout 与基线一致）；"
            "建议 data/evolution/proposals.jsonl（= settings.EVOLUTION_PROPOSAL_LOG）"
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
