#!/usr/bin/env python
"""生产失败样本回流：生成候选 golden 行（**人工确认后**才可并入评估集）。

对应《My Agentix_架构优化方案》§9.1 F7「生产失败轨迹回流评测集」。

设计约束（严格遵守 ``evals/README.md`` §7 回流规范）：

1. **绝不自动改写 ``legal_golden.jsonl``**——本脚本只输出**候选文件**，
   必须人工评审后手动并入（防「改题过关」）。
2. **id 不改**：已存在的 golden id 不会被复用；新候选使用 ``C<序号>`` 前缀
   并在字段中标注 ``needs_review: true``。
3. **只做确定性判定**：哪些生产样本「像失败」由确定性规则判定，不调用 LLM。
4. **不臆造答案**：候选行的 ``expected_sources`` / ``route_type`` 默认留空或
   填系统实际输出，并明确标注「需人工校准」，避免污染 ground truth。

用法::

    # 从生产记录中筛出失败候选（JSONL：每行一条生产记录）
    python evals/feedback_ingest.py --input prod_samples.jsonl

    # 指定输出目录
    python evals/feedback_ingest.py --input prod_samples.jsonl --out-dir evals/candidates
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

EVALS_DIR: Path = Path(__file__).resolve().parent
DEFAULT_GOLDEN: Path = EVALS_DIR / "legal_golden.jsonl"
DEFAULT_CANDIDATES_DIR: Path = EVALS_DIR / "candidates"

#: 视为「失败」的状态（确定性）
FAILURE_STATUSES: frozenset = frozenset({"error", "degraded", "missing_prediction"})

#: 失败类型标签
FAIL_KIND_STATUS = "status_failure"
FAIL_KIND_MISSING_ROUTE = "missing_route"
FAIL_KIND_ERROR_CODE = "error_code"


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    """读取 JSONL；忽略空行与非法行（不静默跳过非法行，而是计数报告）。"""
    rows: List[Dict[str, Any]] = []
    if not Path(path).exists():
        return rows
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def classify_failure(record: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """判定一条生产记录是否属于「失败样本」，返回 ``(kind, reason)`` 或 ``None``。

    确定性规则（不调用 LLM）：
    - ``status`` ∈ {error, degraded} → ``status_failure``
    - 有 ``error_code`` → ``error_code``
    - 既无 ``route_type`` 也无 ``message`` → ``missing_route``
    """
    status = str(record.get("status") or "").lower()
    if record.get("error_code"):
        return FAIL_KIND_ERROR_CODE, f"error_code={record.get('error_code')}"
    if status in FAILURE_STATUSES:
        return FAIL_KIND_STATUS, f"status={status}"
    if not record.get("route_type") and not record.get("message"):
        return FAIL_KIND_MISSING_ROUTE, "无 route_type 且无 message"
    return None


def build_candidate(
    record: Dict[str, Any],
    *,
    candidate_id: str,
    kind: str,
    reason: str,
) -> Dict[str, Any]:
    """把一条失败记录转为候选 golden 行（字段留待人工校准）。"""
    return {
        "id": candidate_id,
        "question": record.get("question") or record.get("query") or "",
        # Phase 31：分层来源可追溯（旧行缺省 legacy 由 runner 兜底）。
        "source": "feedback",
        # ⚠️ 以下字段为「系统实际输出」，仅作参考，**必须人工校准**后才可定稿。
        # 直接把实际输出当期望值会造成「自己考自己」，指标虚高。
        "route_type": record.get("route_type") or None,
        "expected_sources": [],
        "must_contain": [],
        "expect_reject": False,
        # 回流通用元数据
        "_source": {
            "trace_id": record.get("trace_id") or record.get("request_id"),
            "session_id": record.get("session_id"),
            "failure_kind": kind,
            "failure_reason": reason,
            "observed_status": record.get("status"),
            "observed_error_code": record.get("error_code"),
            "captured_at": record.get("timestamp"),
        },
        "_needs_review": True,
        "_review_hint": (
            "请人工确认：① route_type 是否正确；② expected_sources 应填哪些法条；"
            "③ 是否属于越界问题（expect_reject=true）。"
            "确认后去掉下划线前缀字段并手动并入 legal_golden.jsonl（id 不得复用）。"
        ),
    }


def extract_candidates(
    records: Sequence[Dict[str, Any]],
    *,
    start_index: int = 1,
) -> List[Dict[str, Any]]:
    """从生产记录中提取候选 golden 行（纯函数，便于单测）。"""
    candidates: List[Dict[str, Any]] = []
    index = start_index
    for record in records:
        verdict = classify_failure(record)
        if verdict is None:
            continue
        kind, reason = verdict
        candidates.append(
            build_candidate(
                record,
                candidate_id=f"C{index:03d}",
                kind=kind,
                reason=reason,
            )
        )
        index += 1
    return candidates


def next_candidate_index(golden_path: Path) -> int:
    """扫描现有 golden 与候选目录，返回下一个可用的候选序号。"""
    used = 0
    for path in (Path(golden_path),):
        for row in load_jsonl(path):
            cid = str(row.get("id") or "")
            if cid.startswith("C") and cid[1:].isdigit():
                used = max(used, int(cid[1:]))
    return used + 1


def run(
    input_path: Path,
    *,
    out_dir: Path = DEFAULT_CANDIDATES_DIR,
    golden_path: Path = DEFAULT_GOLDEN,
    timestamp: Optional[str] = None,
) -> int:
    """主流程：读入生产记录 → 筛失败 → 写候选文件（**不改 golden**）。"""
    records = load_jsonl(input_path)
    if not records:
        print(f"[回流] 未读到任何记录（或文件不存在）：{input_path}", file=sys.stderr)
        return 2

    candidates = extract_candidates(
        records, start_index=next_candidate_index(golden_path)
    )
    if not candidates:
        print(f"[回流] 共 {len(records)} 条记录，未发现失败样本，无需回流。")
        return 0

    stamp = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{stamp}.candidates.jsonl"
    with out_path.open("w", encoding="utf-8") as handle:
        for candidate in candidates:
            handle.write(json.dumps(candidate, ensure_ascii=False) + "\n")

    print(f"[回流] 输入 {len(records)} 条 → 失败候选 {len(candidates)} 条")
    print(f"[回流] 候选文件：{out_path}")
    print(
        "[回流] ⚠️ 候选文件**不会**自动并入评估集。请人工评审（校准 route_type / "
        "expected_sources / expect_reject）后手动追加到 legal_golden.jsonl。\n"
        "        依据 evals/README.md §7：golden 变更必须人工评审，id 不得复用。"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="feedback_ingest.py",
        description=(
            "生产失败样本回流：生成候选 golden 行（人工确认后才可并入）。\n"
            "本脚本**绝不**修改 legal_golden.jsonl。"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--input",
        required=True,
        help="生产记录 JSONL（每行一条，含 question/route_type/sources/status 等字段）",
    )
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_CANDIDATES_DIR),
        help=f"候选输出目录（默认：{DEFAULT_CANDIDATES_DIR}）",
    )
    parser.add_argument(
        "--golden",
        default=str(DEFAULT_GOLDEN),
        help=f"现有评估集路径（用于计算候选 id 起始序号，默认：{DEFAULT_GOLDEN}）",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return run(
        Path(args.input),
        out_dir=Path(args.out_dir),
        golden_path=Path(args.golden),
    )


if __name__ == "__main__":
    sys.exit(main())
