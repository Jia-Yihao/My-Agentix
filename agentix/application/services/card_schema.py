"""统一卡片 Schema（Phase 32 · M-P0-1，v2 Stage B1）。

画像卡（``long_term_memory.fold_profile_card``）与语义卡
（``semantic_memory.MemoryCard``）此前是**双轨双语义**：冲突行为一边
「同键只标记不覆写」、一边「子串 version+1 覆写」。本模块收敛为一套
Card Schema（字段只增，旧序列化不动）：

- ``CARD_SCHEMA_VERSION``：两卡共用的 schema 版本单源（画像侧
  ``PROFILE_CARD_SCHEMA_VERSION`` 与语义卡 ``MemoryCard.schema_version``
  均取自此）；
- ``canonical_json``：确定性比较/排序用的规范 JSON（键排序、非 ASCII 保留、
  不可序列化退 str）；
- ``conflict_row`` / ``upsert_conflict_row``：**同键冲突只标记不覆写**的
  共用行结构 ``{"key": ..., "values": [...]}`` 与合并语义——values 按
  canonical 去重排序 ⇒ 与输入次序无关；读侧保留行内未知键（只增纪律）。

纪律：纯函数、零第三方、零 I/O（可被 domain/agent/application 任意层引用）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List

__all__ = [
    "CARD_SCHEMA_VERSION",
    "canonical_json",
    "conflict_row",
    "upsert_conflict_row",
]

#: 卡片结构版本（字段结构变化时 +1；读侧对未知版本原样保留，不猜不迁移）。
#: 画像卡与语义卡**共用**——双卡合并（M-P0-1）的版本单源。
CARD_SCHEMA_VERSION = 1


def canonical_json(value: Any) -> str:
    """确定性比较用的规范 JSON 编码（键排序、非 ASCII 保留、不可序列化退 str）。"""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def conflict_row(key: str, values: Iterable[Any]) -> Dict[str, Any]:
    """构造冲突行 ``{"key", "values"}``：values 按 canonical 去重排序。"""
    uniq: Dict[str, Any] = {}
    for value in values:
        uniq[canonical_json(value)] = value
    return {"key": key, "values": [uniq[k] for k in sorted(uniq)]}


def upsert_conflict_row(
    rows: List[Dict[str, Any]], key: str, values: Iterable[Any]
) -> List[Dict[str, Any]]:
    """把 ``values`` 并入 ``rows`` 中 ``key`` 对应的冲突行（无则新建）。

    - **纯函数**：返回新列表，不修改输入（含行 dict）；
    - 同键只保留一行（多值累积进同一行，与画像卡语义一致）；
    - 行内未知键原样保留（只增纪律）。
    """
    out = [dict(row) for row in rows]
    target_index = next(
        (i for i, row in enumerate(out) if row.get("key") == key), None
    )
    if target_index is None:
        out.append(conflict_row(key, values))
        return out
    target = out[target_index]
    merged_values = list(target.get("values") or []) + list(values)
    out[target_index] = {**target, **conflict_row(key, merged_values)}
    return out
