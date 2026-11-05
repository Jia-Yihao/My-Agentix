#!/usr/bin/env python
r"""Ragas 忠实度/相关性独立报告（P2-2，observe-only）。

完全沿 `evals/hybrid_eval.py` 先例：独立脚本、报告键固定、**只增不进门禁**
（绝不进 ci.yml 的 ``METRICS`` 五键元组与 ``evals/baselines/quality.json`` 门键），
缺环境 exit 2 诚实降级——不出假分数。

为什么放 P2 且不进门禁：faithfulness 是**生成质量维度**（答案是否被证据
支撑），judge 端点未校准前无资格参与评分（``evals/README.md`` §4.4 口径）；
本报告只作为独立观察面，与自建确定性指标（路由/引用/拒答）正交。

输入：
- ``--golden``：评估集（默认 ``evals/legal_golden.jsonl``，取 question；该集已于
  2026-10-26 下线删除 ⇒ 不给 ``--golden`` 且默认文件不存在时 exit 2 并列出可用题集，
  不静默跑空，也不新建题集）；
- ``--predictions``：``run_eval.py --record/--offline`` 的预测 JSONL
  （取 message=answer、sources=contexts——sources 是引用溯源串，
  ragas 只需可文本比对的字符串列表，如实使用不二次加工）。

依赖：``ragas``（见 ``requirements-dev.txt``）+ 可用的 judge LLM 端点
（读项目既有 ``LLM_MODEL`` / ``LLM_BASE_URL`` / ``LLM_API_KEY``；
缺任一 ⇒ exit 2，绝不伪造分数）。

用法（PowerShell）::

    .\.venv\Scripts\python.exe evals/ragas_report.py --json-out evals/results/ragas_report.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

EVALS_DIR: Path = Path(__file__).resolve().parent
DEFAULT_GOLDEN: Path = EVALS_DIR / "legal_golden.jsonl"
DEFAULT_PREDICTIONS: Path = EVALS_DIR / "baseline_predictions.jsonl"

# T3：默认题集缺失 ⇒ 响亮退出。判据与其余入口同源，不各写一份可用集清单。
try:
    from golden_source import ensure_default_golden
except ModuleNotFoundError as _gs_exc:  # pragma: no cover - 包名路径（pytest / -m 调用）
    if _gs_exc.name != "golden_source":
        raise
    from evals.golden_source import ensure_default_golden

#: 报告键族（固定；守卫测试 tests/test_p106_ragas_report.py 钉死，
#: 并断言与本报告键族和 CI METRICS 五键的交集为空）
REPORT_KEYS = (
    "ragas_faithfulness",
    "ragas_answer_relevancy",
    "ragas_questions",
    "ragas_unavailable",
)


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def build_samples(
    golden_rows: Sequence[Dict[str, Any]],
    prediction_rows: Sequence[Dict[str, Any]],
    *,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """golden × 预测按 id 对齐 → ragas 评估样本（纯函数，便于单测）。

    - 每个 id 取 attempt 最小的一条预测（与 run_eval 首轮语义一致）；
    - ``contexts`` = 预测行 ``sources`` 的字符串化列表；sources 为空的样本
      跳过并计入 unavailable（faithfulness 无证据可验，硬造 contexts 即伪造）；
    - 空答案同样跳过（answer_relevancy 无从谈起）。
    """
    by_id: Dict[str, Dict[str, Any]] = {}
    for row in prediction_rows:
        gid = str(row.get("id") or "")
        if not gid:
            continue
        prev = by_id.get(gid)
        if prev is None or int(row.get("attempt") or 0) < int(prev.get("attempt") or 0):
            by_id[gid] = row

    unavailable: List[str] = []
    samples: List[Dict[str, Any]] = []
    for g in golden_rows:
        gid = str(g.get("id") or "")
        question = str(g.get("question") or "").strip()
        pred = by_id.get(gid)
        if pred is None:
            unavailable.append(f"{gid}:no_prediction")
            continue
        answer = str(pred.get("message") or "").strip()
        contexts = [str(s) for s in (pred.get("sources") or []) if str(s).strip()]
        if not question or not answer or not contexts:
            unavailable.append(f"{gid}:empty_field")
            continue
        samples.append({"question": question, "answer": answer, "contexts": contexts})
        if limit is not None and len(samples) >= limit:
            break
    # 全部无预测的 golden 不逐条罗列（噪声），只在 unavailable 计数可见
    no_pred = sum(1 for u in unavailable if u.endswith(":no_prediction"))
    if no_pred > 0 and limit is None:
        unavailable = [u for u in unavailable if not u.endswith(":no_prediction")]
        if no_pred:
            unavailable.insert(0, f"{no_pred} 条 golden 无对应预测（整体计数）")
    return samples


def _resolve_report(samples: List[Dict[str, Any]], scores: Dict[str, Any]) -> Dict[str, Any]:
    """固定键族报告（只增不进门禁）。"""
    return {
        "ragas_faithfulness": scores.get("faithfulness"),
        "ragas_answer_relevancy": scores.get("answer_relevancy"),
        "ragas_questions": len(samples),
        "ragas_unavailable": scores.get("unavailable", []),
    }


def run(args: argparse.Namespace) -> int:
    # -- 依赖与环境前置（任何缺失 ⇒ exit 2 诚实降级，不出假表） --------
    try:
        from ragas import evaluate  # noqa: F401
        from ragas.metrics import answer_relevancy, faithfulness  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - 缺依赖/依赖损坏同径诚实降级
        print(f"[ragas] ragas 依赖不可用（{type(exc).__name__}: {exc}）；"
              "本报告 observe-only，缺环境不出假分数。安装见 requirements-dev.txt。",
              file=sys.stderr)
        return 2

    from agentix.config import settings as settings_mod

    base_url = settings_mod.LLM_BASE_URL or os.environ.get("OPENAI_BASE_URL") or ""
    api_key = settings_mod.LLM_API_KEY or os.environ.get("OPENAI_API_KEY") or ""
    model = settings_mod.LLM_MODEL or ""
    if not (base_url and api_key and model):
        print("[ragas] judge 端点缺失（需 LLM_MODEL/LLM_BASE_URL/LLM_API_KEY 或 "
              "OPENAI_BASE_URL/OPENAI_API_KEY）——缺环境 exit 2，不伪造分数。",
              file=sys.stderr)
        return 2

    golden_rows = _load_jsonl(Path(args.golden))
    pred_rows = _load_jsonl(Path(args.predictions))
    if not golden_rows or not pred_rows:
        print(f"[ragas] 输入缺失：golden={len(golden_rows)} 行, "
              f"predictions={len(pred_rows)} 行（先跑 run_eval --record）。",
              file=sys.stderr)
        return 2

    samples = build_samples(golden_rows, pred_rows, limit=args.limit)
    if not samples:
        print("[ragas] 无可评估样本（预测缺 sources/message）——不出假表。", file=sys.stderr)
        return 2

    # -- ragas 评估（lazy import 到此处；运行期异常同径 exit 2） --------
    try:
        from datasets import Dataset
        from langchain_openai import ChatOpenAI
        from ragas.metrics import answer_relevancy, faithfulness
        from ragas import evaluate
        from ragas.llms import LangchainLLMWrapper

        judge = LangchainLLMWrapper(
            ChatOpenAI(
                model=model,
                base_url=base_url,
                api_key=api_key,
                temperature=0,
            )
        )
        ds = Dataset.from_list(samples)
        result = evaluate(
            ds,
            metrics=[faithfulness, answer_relevancy],
            llm=judge,
        )
        raw = dict(result)
        scores = {
            "faithfulness": raw.get("faithfulness"),
            "answer_relevancy": raw.get("answer_relevancy"),
            "unavailable": [],
        }
    except Exception as exc:  # noqa: BLE001 - 工具脚本：诚实报错退出
        print(f"[ragas] 评估失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    report = _resolve_report(samples, scores)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                       encoding="utf-8")
        print(f"[ragas] 报告已写入 {out}", file=sys.stderr)
    print("[ragas] ⚠️ 本报告 observe-only：ragas_* 键族不进门禁（CI METRICS 与 "
          "quality.json 零接触）。", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ragas_report.py",
        description="Ragas faithfulness/answer_relevancy 独立报告（observe-only，缺环境 exit 2）。",
    )
    parser.add_argument(
        "--golden",
        default=None,
        help=f"评估集 JSONL（默认 {DEFAULT_GOLDEN.name}；该集已下线时必须显式指定）",
    )
    parser.add_argument("--predictions", default=str(DEFAULT_PREDICTIONS), help="预测 JSONL（run_eval 产物）")
    parser.add_argument("--json-out", default=None, help="报告落盘路径（可选）")
    parser.add_argument("--limit", type=int, default=None, help="最多评估 N 个样本（试点/省钱）")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # 缺省题集不存在 ⇒ 非零退出 + 可用题集清单 + 改道示例（T3）；显式传值行为不变。
    golden, rc = ensure_default_golden(
        args.golden,
        entry="evals/ragas_report.py",
        default=DEFAULT_GOLDEN,
        example_flags="--predictions <预测文件>",
    )
    if rc:
        return rc
    args.golden = str(golden)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
