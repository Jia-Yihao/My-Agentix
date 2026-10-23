"""Normalisation helpers for legal-source metadata.

The keys in this module are deliberately stable: they are part of the
retrieval API contract and can be used by answer renderers without knowing
which source database produced a document.

R2-2b（指南 v2.0 §2.2.7①）：在旧三键（legal_effective_from/to、
source_authority_level）基础上新增四字段有效性契约——

    effective_status : "in_force" | "pending" | "expired" | "repealed"
    effective_date   : 生效日期（ISO，别名 legal_effective_from）
    expire_date      : 失效日期（ISO，含当天有效；别名 legal_effective_to）
    replaced_by      : 被何法替代（纯展示）

``effective_status`` 缺失时由日期推导（与检索侧
``agentix.retrieval.domain.policies.legal_effectiveness`` 语义一致）：
生效日在未来 → pending；失效日已过（严格大于）→ expired；否则 in_force。
旧键继续保留写入，兼容既有消费方。
"""

from datetime import date, datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo

#: 本模块判定"今天"所用时区（2026-09 修复）。
#:
#: **必须与检索侧** ``agentix.retrieval.domain.policies.legal_effectiveness._LEGAL_TZ``
#: **及** ``agentix.config.settings.DISPLAY_TZ`` **一致**——本模块是**入库侧**
#: 有效性推导，检索侧是**查询侧**推导，两者判同一条数据条目必须得出同一结论。
#: 原实现用 ``date.today()``（进程本地时区）：容器 ``TZ`` 未设置时为 UTC，
#: 与 +08:00 相差 8 小时，会在北京时间 00:00–08:00 之间让**两侧对同一条数据条目
#: 给出不同的 status**——而本模块顶部注释声称"与检索侧语义一致"，实际不一致。
#:
#: ``kb_ingest`` 是独立包（全包 0 处 import agentix），故此处重复**字面量**
#: 而非真源；三方一致性由 ``tests/test_p42_time_discipline.py`` 强制。
_LEGAL_TZ = ZoneInfo("Asia/Shanghai")


EFFECTIVE_FROM = "legal_effective_from"
EFFECTIVE_TO = "legal_effective_to"
AUTHORITY_LEVEL = "source_authority_level"

# R2-2b 四字段有效性契约（检索侧读取的规范名）
EFFECTIVE_STATUS = "effective_status"
EFFECTIVE_DATE = "effective_date"
EXPIRE_DATE = "expire_date"
REPLACED_BY = "replaced_by"

_STATUS_ALIASES = {
    "in_force": "in_force",
    "effective": "in_force",
    "现行有效": "in_force",
    "有效": "in_force",
    "pending": "pending",
    "未生效": "pending",
    "expired": "expired",
    "已失效": "expired",
    "失效": "expired",
    "repealed": "repealed",
    "已废止": "repealed",
    "废止": "repealed",
}


def _iso_date(value: Any) -> str | None:
    """宽容转 ISO 日期字符串：date/datetime/ISO 字符串；失败返回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()[:10]
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return None
    return None


def _derive_status(effective: str | None, expire: str | None, today: date | None = None) -> str:
    """由日期推导有效性状态（与检索侧 effective_status_of 推导分支一致）。

    ``today`` 可注入以便测试；缺省取 ``_LEGAL_TZ``（+08:00）的今天。
    """
    today = today or datetime.now(_LEGAL_TZ).date()
    if effective is not None:
        try:
            if today < date.fromisoformat(effective):
                return "pending"
        except ValueError:
            pass
    if expire is not None:
        try:
            if today > date.fromisoformat(expire):
                return "expired"
        except ValueError:
            pass
    return "in_force"


def normalise_legal_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return metadata with canonical legal date/authority/effectiveness keys.

    Existing aliases are accepted to keep old ingestion jobs compatible.
    Dates are stored as ISO dates, making them safe to compare in PostgreSQL
    JSONB and straightforward for answer clients to display.
    """
    result = dict(metadata or {})
    aliases = {
        EFFECTIVE_FROM: ("effective_from", "valid_from", "生效日期", "有效起始日期"),
        EFFECTIVE_TO: ("effective_to", "valid_to", "失效日期", "有效截止日期"),
        AUTHORITY_LEVEL: ("authority_level", "source_authority", "来源权威等级", "权威等级"),
        EFFECTIVE_DATE: ("生效日期", "legal_effective_from"),
        EXPIRE_DATE: ("失效日期", "legal_effective_to"),
        REPLACED_BY: ("被替代法", "替代法", "被何法替代"),
    }
    for canonical, candidates in aliases.items():
        if result.get(canonical) in (None, ""):
            for candidate in candidates:
                if result.get(candidate) not in (None, ""):
                    result[canonical] = result[candidate]
                    break
    for key in (EFFECTIVE_FROM, EFFECTIVE_TO, EFFECTIVE_DATE, EXPIRE_DATE):
        value = result.get(key)
        if isinstance(value, (datetime, date)):
            result[key] = value.isoformat()

    # R2-2b：有效性状态——显式值归一化；缺失时由日期推导（fail-open：日期坏 → in_force）
    raw_status = result.get(EFFECTIVE_STATUS)
    if isinstance(raw_status, str) and raw_status.strip():
        result[EFFECTIVE_STATUS] = _STATUS_ALIASES.get(raw_status.strip().lower(), raw_status.strip().lower())
    else:
        effective = _iso_date(result.get(EFFECTIVE_DATE)) or _iso_date(result.get(EFFECTIVE_FROM))
        expire = _iso_date(result.get(EXPIRE_DATE)) or _iso_date(result.get(EFFECTIVE_TO))
        result[EFFECTIVE_STATUS] = _derive_status(effective, expire)

    # 日期互镜像：规范四字段 ↔ 旧键双向补齐，检索/展示两侧读哪个都一致
    if result.get(EFFECTIVE_DATE) in (None, "") and result.get(EFFECTIVE_FROM) not in (None, ""):
        result[EFFECTIVE_DATE] = result[EFFECTIVE_FROM]
    if result.get(EFFECTIVE_FROM) in (None, "") and result.get(EFFECTIVE_DATE) not in (None, ""):
        result[EFFECTIVE_FROM] = result[EFFECTIVE_DATE]
    if result.get(EXPIRE_DATE) in (None, "") and result.get(EFFECTIVE_TO) not in (None, ""):
        result[EXPIRE_DATE] = result[EFFECTIVE_TO]
    if result.get(EFFECTIVE_TO) in (None, "") and result.get(EXPIRE_DATE) not in (None, ""):
        result[EFFECTIVE_TO] = result[EXPIRE_DATE]

    return result
