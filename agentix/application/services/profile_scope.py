"""F2a·M-P2-4（memory-optimization §6 画像作用域）：画像字段分级 + 投影（纯函数）。

- **分级单源**：``PROFILE_FIELD_TIERS`` —— ``stable``（长期稳定，跨会话沿用
  需授权）/ ``session``（会话偏好与统计，任何会话可注）/ ``meta``（结构元
  字段，永远保留）；
- **fail-closed**：未知字段一律按 ``stable``（新字段默认需要授权，不猜）；
- **投影**：``project_profile_card`` / ``project_profile_topics`` ——
  ``allow_stable=False``（未授权跨会话沿用）时只保留会话层 + 元字段；
  ``allow_stable=True`` 时原样拷贝（与注入基线字节一致）。

接线：``long_term_memory.get_context_for_query`` 在
``MEMORY_PROFILE_SCOPE_ENABLED`` 开启时按授权结果投影（未授权 fail-closed），
本模块零 I/O 可离线单测。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence

__all__ = [
    "PROFILE_FIELD_TIERS",
    "PROFILE_META_FIELDS",
    "TIER_STABLE",
    "TIER_SESSION",
    "TIER_META",
    "tier_of",
    "project_profile_card",
    "project_profile_topics",
]

TIER_STABLE = "stable"
TIER_SESSION = "session"
TIER_META = "meta"

#: 画像卡字段分级（单源真源；注入侧按此投影）。
#: - ``stable``：user_constraints / legal_domain_prefs / conflicts ——
#:   跨会话稳定约束与观测（沿用需授权）；
#: - ``session``：usage_habits —— 使用统计（随会话走）；
#: - ``meta``：schema_version —— 结构元字段（永远保留）。
PROFILE_FIELD_TIERS: Mapping[str, str] = {
    "schema_version": TIER_META,
    "usage_habits": TIER_SESSION,
    "legal_domain_prefs": TIER_STABLE,
    "user_constraints": TIER_STABLE,
    "conflicts": TIER_STABLE,
}

#: 永远保留的结构元字段。
PROFILE_META_FIELDS = frozenset({"schema_version"})


def tier_of(field: str) -> str:
    """字段分级：未知字段 ⇒ ``stable``（fail-closed，新字段需授权才跨会话沿用）。"""
    return PROFILE_FIELD_TIERS.get(field, TIER_STABLE)


def project_profile_card(
    card: Any, *, allow_stable: bool
) -> Dict[str, Any]:
    """按授权结果投影画像卡（纯函数，不改输入）。

    - ``allow_stable=True``：浅拷贝原卡（与未分级注入字节一致）；
    - ``allow_stable=False``：只保留 ``meta``/``session`` 层字段（稳定层
      跨会话沿用被扣下——授权边界 fail-closed）；
    - 非 dict 输入 ⇒ 空卡（防御，不抛）。
    """
    if not isinstance(card, dict):
        return {}
    if allow_stable:
        return dict(card)
    return {
        key: value
        for key, value in card.items()
        if tier_of(key) in (TIER_META, TIER_SESSION)
    }


def project_profile_topics(
    topics: Sequence[str], *, allow_stable: bool
) -> List[str]:
    """频繁领域（长期稳定字段）投影：未授权 ⇒ 空列表（调用方回落「未记录」）。"""
    if allow_stable:
        return list(topics)
    return []
