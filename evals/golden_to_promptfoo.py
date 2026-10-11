#!/usr/bin/env python
r"""golden → promptfoo tests 转换脚本（P1-1.5/P1-5）。

把 ``evals/legal_golden.jsonl`` 机械转换成 promptfoo 用例 JSONL（经
``promptfooconfig.yaml`` 的 ``tests: file://…`` 引用）：question → vars，
route_type / expect_reject / expected_sources / must_contain → 断言参数。
**转换是纯机械映射，不产生任何新的期望值**（期望只能来自 golden 单源）。

设计要点：

- **试点规模**：默认 ``--limit 25``（清单口径 20–30 条试点跑通再全量；
  全量传 ``--limit 0`` = 不限）。**取前 N 条而非随机抽样**——转换结果
  可复现，守卫测试（test_p104）才能做"转换产物条数 == golden 子集条数"的
  确定性断言。
- 断言**不写死在本文件**：期望参数放进 vars，由
  ``evals/promptfoo_asserts.py::get_assert`` 消费（打分函数直接 import
  run_eval，口径单源）。

用法（PowerShell）::

    .\.venv\Scripts\python.exe evals/golden_to_promptfoo.py --out evals/promptfoo_tests.jsonl --limit 25
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

EVALS_DIR: Path = Path(__file__).resolve().parent
DEFAULT_GOLDEN: Path = EVALS_DIR / "legal_golden.jsonl"
DEFAULT_OUT: Path = EVALS_DIR / "promptfoo_tests.jsonl"


def golden_row_to_test(row: Dict[str, Any]) -> Dict[str, Any]:
    """单条 golden → promptfoo 用例（纯函数；机械映射，零语义加工）。"""
    return {
        "description": str(row.get("id") or ""),
        "vars": {
            "question": str(row.get("question") or ""),
            # 期望参数统一字符串化（promptfoo vars 面向 YAML/JSON 序列化）
            "expected_route": str(row.get("route_type") or ""),
            "expect_reject": "true" if row.get("expect_reject") else "false",
            "expected_sources": json.dumps(
                [str(s) for s in (row.get("expected_sources") or [])], ensure_ascii=False
            ),
            "must_contain": json.dumps(
                [str(k) for k in (row.get("must_contain") or [])], ensure_ascii=False
            ),
        },
        # 断言统一走自定义 python 断言文件（get_assert → run_eval 打分函数）
        "assert": [
            {
                "type": "python",
                "value": "file://evals/promptfoo_asserts.py",
            }
        ],
    }


def convert(
    golden_rows: Sequence[Dict[str, Any]],
    *,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """转换前 N 条 golden（limit=0/None = 全量；取前 N 保证可复现）。"""
    tests: List[Dict[str, Any]] = []
    for row in golden_rows:
        if limit is not None and limit > 0 and len(tests) >= limit:
            break
        tests.append(golden_row_to_test(row))
    return tests


def run(golden_path: Path, out_path: Path, *, limit: Optional[int]) -> int:
    rows: List[Dict[str, Any]] = []
    for line in golden_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not rows:
        print(f"[转换] golden 为空或不存在：{golden_path}", file=sys.stderr)
        return 2
    tests = convert(rows, limit=limit)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for t in tests:
            handle.write(json.dumps(t, ensure_ascii=False) + "\n")
    print(f"[转换] golden {len(rows)} 条 → promptfoo 用例 {len(tests)} 条 → {out_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="golden_to_promptfoo.py",
        description="legal_golden.jsonl → promptfoo tests JSONL（机械映射，期望零加工）。",
    )
    parser.add_argument("--golden", default=str(DEFAULT_GOLDEN))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--limit", type=int, default=25,
                        help="试点条数（默认 25；0 = 全量 102 条）")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    limit = args.limit if args.limit and args.limit > 0 else None
    return run(Path(args.golden), Path(args.out), limit=limit)


if __name__ == "__main__":
    sys.exit(main())
