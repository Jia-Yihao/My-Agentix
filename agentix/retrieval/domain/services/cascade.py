"""确定性查询分类 + 低分升级的检索级联（adaptive cascade）。

领域层纯件（不 import 向量库/Redis/LLM 等基础设施）：
- ``classify_query_kind``：无 LLM 的确定性三分类（statute / multi_hop /
  general）——「第X条」→ statute（exact/直取优先）；对比/异同类 → multi_hop
  （graph 优先，graph 未注入时由用例层排 hybrid 前置）；其余 general；
- ``CascadeLevel`` + ``run_cascade``：按序执行注入的各级检索回调，
  ``min_score>0`` 时按归一化分数设闸——空命中或低于闸 ⇒ 升级下一级；
  单级抛异常 fail-open 记录后继续；全部耗尽时回退**历史最优**（有结果则
  绝不因升级失败而空手，全空且有异常才重抛末次异常）。

分数口径（``normalize_level_score``）：
- 可测分数（文档带 ``_retrieval_score``）：``higher_is_better=True`` 取最大
  原分（融合分/相似度）；``False`` 按距离口径换算 ``1/(1+min)``；
- 不可测（无分数字段）⇒ 返回 ``None``，用例层**视为过闸**（无法判定质量
  时不无限升级，fail-open 停在当前级）。

接线：``retrieve_scoped_docs_cached`` 在 ``RETRIEVAL_ADAPTIVE_ENABLED=true``
（默认关）时组装级别；flag-off 时本模块不被导入，检索路径与基线逐位等价。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

# 「第X条」（X = 阿拉伯数字或中文数字）——数据条目直取类问题
_STATUTE_RE = re.compile(r"第\s*[0-9一二三四五六七八九十百千]+\s*条")
# 多跳/对比类问题（与 graph 多实体关系推导对应）
_MULTI_HOP_MARKERS: Tuple[str, ...] = (
    "对比",
    "异同",
    "区别",
    "关系",
    "分别",
    "两者",
    "以及",
)

KindName = str  # "statute" | "multi_hop" | "general"


def classify_query_kind(question: str) -> KindName:
    """确定性查询分类（无 LLM、无 I/O；顺序：statute > multi_hop > general）。"""
    q = (question or "").strip()
    if not q:
        return "general"
    if _STATUTE_RE.search(q):
        return "statute"
    if any(marker in q for marker in _MULTI_HOP_MARKERS):
        return "multi_hop"
    return "general"


@dataclass
class CascadeLevel:
    """级联一级：名字 + 注入的异步检索动作 + 分数方向口径。"""

    name: str
    run: Callable[[], Awaitable[list]]
    higher_is_better: bool = True


def normalize_level_score(
    docs: list, *, higher_is_better: bool
) -> Optional[float]:
    """把一级结果归一化到「越大越相关」的可比分（None = 无分数可测）。"""
    if not docs:
        return float("-inf")
    best: Optional[float] = None
    for doc in docs:
        meta = getattr(doc, "metadata", None) or {}
        try:
            raw = float(meta.get("_retrieval_score"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if higher_is_better:
            cand = raw
        else:
            cand = 1.0 / (1.0 + max(raw, 0.0))
        if best is None or cand > best:
            best = cand
    return best


async def run_cascade(
    levels: List[CascadeLevel], *, min_score: float
) -> Tuple[list, Dict[str, Any]]:
    """按序跑级联，分数闸未过/空命中则升级；返回 ``(docs, report)``。

    - ``min_score <= 0``：闸关——首个**非空**结果即停（空/异常才升级）；
    - 全部耗尽：有历史最优 ⇒ 回退返回（gate=exhausted_fallback）；
      全空且存在异常 ⇒ 重抛末次异常；全空无异常 ⇒ 空结果。
    """
    attempts: List[Dict[str, Any]] = []
    best_docs: list = []
    best_norm: float = float("-inf")
    best_level: Optional[str] = None
    last_error: Optional[BaseException] = None

    for level in levels:
        try:
            docs = await level.run()
        except Exception as exc:  # noqa: BLE001 - 单级失败 fail-open 升级
            attempts.append({"level": level.name, "error": type(exc).__name__})
            last_error = exc
            continue
        docs = docs or []
        norm = normalize_level_score(
            docs, higher_is_better=level.higher_is_better
        )
        attempts.append(
            {
                "level": level.name,
                "docs": len(docs),
                "score": norm,
            }
        )
        if docs and norm is not None and norm > best_norm:
            best_docs, best_norm, best_level = docs, norm, level.name
        elif docs and best_level is None:
            # 无分数可测：仍记为候选（回退兜底），但视为过闸
            best_docs, best_level = docs, level.name
        if docs and (min_score <= 0 or norm is None or norm >= min_score):
            return docs, {
                "attempts": attempts,
                "stopped_at": level.name,
                "gate": "pass",
            }

    if best_docs:
        return best_docs, {
            "attempts": attempts,
            "stopped_at": best_level,
            "gate": "exhausted_fallback",
        }
    if last_error is not None:
        raise last_error
    return [], {
        "attempts": attempts,
        "stopped_at": None,
        "gate": "exhausted_empty",
    }
