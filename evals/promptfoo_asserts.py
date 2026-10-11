#!/usr/bin/env python
"""promptfoo 自定义断言（P1-5）：打分口径**单源**——直接 import run_eval 的
确定性打分函数，绝不抄写第二份（抄了就会两边口径漂移，守卫测试钉死这点）。

promptfoo 断言约定：断言文件定义 ``get_assert(output, context)``，返回
``bool | float | {"pass": bool, "score": float, "reason": str}``。

本断言读 provider 输出的结构化 JSON（route/sources/message/status），
按用例 vars 里转换脚本写入的期望参数（expected_route / expect_reject /
expected_sources / must_contain，均经 ``golden_to_promptfoo.py`` 从
``legal_golden.jsonl`` 机械转换）逐维度打分，取均值作为 score。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_REPO_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 打分口径单源：直接 import run_eval（evals/ 侧真源），不复制实现
from evals.run_eval import (  # noqa: E402 - sys.path 注入后 import
    score_citations,
    score_must_contain,
    score_reject,
    score_route,
)


def get_assert(output: str, context: Any) -> Dict[str, Any]:
    """按用例期望逐维度打分（确定性，无 LLM）。"""
    vars_ = getattr(context, "vars", None) or (context or {}).get("vars", {})

    try:
        parsed = json.loads(output or "{}")
    except json.JSONDecodeError:
        return {"pass": False, "score": 0.0, "reason": "provider 输出不是合法 JSON"}

    if not isinstance(parsed, dict) or parsed.get("error"):
        return {"pass": False, "score": 0.0,
                "reason": f"provider 调用失败：{parsed.get('error', 'unknown')}"}

    expected_route = vars_.get("expected_route") or None
    if expected_route in ("", "null", "none", "None"):
        expected_route = None
    expect_reject = str(vars_.get("expect_reject", "")).lower() == "true"
    expected_sources = _parse_list(vars_.get("expected_sources"))
    must_contain = _parse_list(vars_.get("must_contain"))

    sources = parsed.get("sources") or []
    message = parsed.get("message") or ""

    checks: Dict[str, Any] = {}
    if expected_route is not None or "expected_route" in vars_:
        checks["route"] = score_route(expected_route, parsed.get("route"))
    if expect_reject or "expect_reject" in vars_:
        checks["reject"] = score_reject(expect_reject, parsed.get("status"), message, sources)
    if expected_sources:
        checks["citations"] = score_citations(expected_sources, sources)
    if must_contain:
        checks["must_contain"] = score_must_contain(must_contain, message)

    if not checks:
        return {"pass": True, "score": 1.0, "reason": "该用例无可判定维度（vacuous）"}

    score = sum(float(v) for v in checks.values()) / len(checks)
    failed = [k for k, v in checks.items() if not float(v) >= 1.0]
    return {
        "pass": score >= 1.0,
        "score": round(score, 4),
        "reason": "全部维度命中" if not failed else f"未满分维度：{failed}（明细 {checks}）",
    }


def _parse_list(raw: Any) -> list:
    """vars 里的列表字段：转换脚本写成 JSON 串或分号分隔串，两种都认。"""
    if raw is None or raw == "":
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw]
    text = str(raw).strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except json.JSONDecodeError:
            pass
    return [seg for seg in (s.strip() for s in text.split(";")) if seg]


if __name__ == "__main__":
    # 本地自检：echo '<provider 输出 JSON>' | python evals/promptfoo_asserts.py
    raw = sys.stdin.read()
    ctx_vars = {
        "expected_route": "kb-query",
        "expected_sources": json.dumps(["民法典"]),
        "must_contain": "",
        "expect_reject": "false",
    }
    print(json.dumps(get_assert(raw, {"vars": ctx_vars}), ensure_ascii=False))
