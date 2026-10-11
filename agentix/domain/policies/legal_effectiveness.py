"""法条效力过滤纯函数核心（R2-2，指南 v2.0 §2.2.7①）。

四字段效力契约（写入 chunk/document metadata）::

    effective_status : str  "in_force" | "expired" | "repealed"（缺失 → 按日期推导）
    effective_date   : str  生效日期（ISO 日期；别名 legal_effective_from/生效日期）
    expire_date      : str  失效日期（ISO 日期，当天仍有效；别名 legal_effective_to/失效日期）
    replaced_by      : str  被何法替代（仅展示用途，不参与过滤判定）

设计约束：
- **存量数据默认有效**：现有 1953 条法条 metadata 无效力字段 → 一律按 in_force 处理，
  无需重建索引。
- **宁可漏报不误报**（与注入检测同原则）：metadata 日期解析失败 → 视为有效，
  绝不因脏数据误杀法条。
- **后过滤而非谓词下推**：milvus/qdrant 后端 filter 仅支持 dict 等值匹配，
  无法表达"缺失字段视为有效"；故统一在检索结果上做纯函数后过滤，
  faiss/milvus/qdrant 三后端行为一致。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo

#: 本域判定"今天"所用时区。
#:
#: **为什么在这里再声明一次，而不是 import ``agentix.config.settings``**：
#: ``domain`` 层不得依赖任何上层/配置实现——``agentix.config.settings`` 在
#: 导入期就会实例化 pydantic ``Settings()`` 并读取 ``.env``，把领域纯函数
#: 绑死到运行环境上，破坏可独立测试性（本模块的 ``now`` 全部可注入正是为此）。
#:
#: 与 ``agentix.config.settings.DISPLAY_TZ`` 取值必须一致；该一致性由
#: ``tests/test_p42_time_discipline.py`` 的守卫用例强制（发现漂移即失败），
#: 因此这里重复的是**字面量**而非**真源**。
_LEGAL_TZ = ZoneInfo("Asia/Shanghai")

# 显式状态值（中英文均接受，归一化后判定）
STATUS_IN_FORCE = "in_force"
STATUS_EXPIRED = "expired"
STATUS_REPEALED = "repealed"
STATUS_PENDING = "pending"  # 未生效（effective_date 在未来）

_IN_FORCE_ALIASES = {"in_force", "effective", "现行有效", "有效"}
_EXPIRED_ALIASES = {"expired", "repealed", "已失效", "已废止", "废止", "失效"}

# 日期字段候选（规范名在前，入库侧别名在后——与 kb_ingest legal_metadata.py 对齐）
_EFFECTIVE_DATE_KEYS = ("effective_date", "legal_effective_from", "生效日期")
_EXPIRE_DATE_KEYS = ("expire_date", "legal_effective_to", "失效日期")


def _parse_date(value: Any) -> date | None:
    """宽容解析日期：date/datetime/ISO 字符串；失败返回 None（fail-open）。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()[:10]  # 兼容 "2021-01-01T00:00:00" / "2021-01-01 00:00:00"
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    return None


def _as_date(now: date | datetime | None) -> date:
    if now is None:
        # 2026-09 修复：原为 ``date.today()``——取**进程本地时区**的日期。
        # 容器部署时 ``TZ`` 通常未设置（=UTC），与北京时间相差 8 小时：
        # 北京时间 00:00–08:00 之间"今天"会算成昨天，使下面判定顺序 2 的
        # 两处边界（effective_date 未到 → pending、expire_date 已过 → expired）
        # 整体错位一天，且 ``format_retrieved_asof`` 输出的"检索截至"日期也错。
        # 法规效力判定属法律语义，不能容忍这种偏差。见模块顶部 _LEGAL_TZ 说明。
        return datetime.now(_LEGAL_TZ).date()
    if isinstance(now, datetime):
        return now.date()
    return now


def effective_status_of(meta: Mapping[str, Any] | None, *, now: date | datetime | None = None) -> str:
    """判定单条 metadata 的效力状态（纯函数）。

    判定顺序：
    1. 显式 ``effective_status``：in_force 别名 → in_force；expired/repealed 别名 → 对应状态；
    2. 无显式状态（或不可识别）→ 日期推导：effective_date 在未来 → pending；
       expire_date 已过（严格大于，失效当天仍有效）→ expired；否则 in_force；
    3. 字段全缺或解析失败 → in_force（存量默认有效 + fail-open）。
    """
    today = _as_date(now)
    raw_status = (meta or {}).get("effective_status")
    if isinstance(raw_status, str) and raw_status.strip():
        normalized = raw_status.strip().lower()
        if normalized in _IN_FORCE_ALIASES:
            return STATUS_IN_FORCE
        if normalized in _EXPIRED_ALIASES:
            return STATUS_REPEALED if normalized in ("repealed", "已废止", "废止") else STATUS_EXPIRED
    # 日期推导
    for key in _EFFECTIVE_DATE_KEYS:
        effective = _parse_date((meta or {}).get(key))
        if effective is not None and today < effective:
            return STATUS_PENDING
    for key in _EXPIRE_DATE_KEYS:
        expire = _parse_date((meta or {}).get(key))
        if expire is not None and today > expire:
            return STATUS_EXPIRED
    return STATUS_IN_FORCE


def is_in_force(meta: Mapping[str, Any] | None, *, now: date | datetime | None = None) -> bool:
    """该条 metadata 是否现行有效（检索过滤谓词）。缺失字段/解析失败一律 True。"""
    return effective_status_of(meta, now=now) == STATUS_IN_FORCE


def filter_in_force(
    pairs: list[tuple],
    *,
    now: date | datetime | None = None,
    enabled: bool = True,
) -> list[tuple]:
    """过滤 (doc, score) 序列中已失效/未生效的条目（纯函数后过滤）。

    - ``enabled=False`` 原样返回（开关：EFFECTIVENESS_FILTER_ENABLED）；
    - doc.metadata 判定走 :func:`is_in_force`（缺失字段视为有效）；
    - metadata 访问异常视为有效（fail-open，不因单条脏数据中断整批）。
    """
    if not enabled:
        return pairs
    kept: list[tuple] = []
    for doc, score in pairs:
        try:
            if is_in_force(getattr(doc, "metadata", None) or {}, now=now):
                kept.append((doc, score))
        except Exception:  # noqa: BLE001 - 单条判定异常不阻断整批
            kept.append((doc, score))
    return kept


def format_retrieved_asof(now: date | datetime | None = None) -> str:
    """答案尾部"检索截至日期"标注行（指南 §2.2.7①）。"""
    return f"检索截至 {_as_date(now).isoformat()}"


def append_retrieved_asof(answer_text: str, *, now: date | datetime | None = None) -> str:
    """在答案尾部追加检索截至标注（引用核验标注之前保留原答案主体）。"""
    return f"{answer_text}\n\n---\n> 📅 {format_retrieved_asof(now)}，法律法规如有更新请以最新发布文本为准。"


def attach_effective_status(hit: dict, meta: Mapping[str, Any] | None) -> dict:
    """向 sources/hit_chunks 字典补 ``effective_status``（带 replaced_by 便于前端引用卡展示）。"""
    hit = dict(hit)
    hit["effective_status"] = effective_status_of(meta)
    replaced = (meta or {}).get("replaced_by")
    if replaced:
        hit["replaced_by"] = str(replaced)
    return hit


# ---------------------------------------------------------------------------
# R2-2 收口：KG/Cypher 记录级效力后过滤（Neo4j 路径）
# ---------------------------------------------------------------------------

def filter_records_in_force(
    records: list,
    *,
    now: date | datetime | None = None,
    enabled: bool = True,
) -> list:
    """过滤 Neo4j/KG 查询记录（List[Dict]）中已失效/未生效的条目。

    与 :func:`filter_in_force` 同语义，但作用于**任意 dict 记录**
    （Cypher 查询返回结构不定，无法假定 (doc, score) 形态）：

    - 记录**无任何效力字段** → 原样保留且不注入任何字段
      （统计/聚合类查询结果字节级零变化——存量图数据无效力字段，
      本函数对它们是 no-op）；
    - 记录携带效力字段且已失效/未生效 → 剔除；
    - 记录携带效力字段且现行有效 → 保留并回写 ``effective_status``（展示用）；
    - 非 dict 记录原样保留；单条判定异常不阻断整批（fail-open）。

    ``enabled=False`` 原样返回（开关复用 EFFECTIVENESS_FILTER_ENABLED）。
    """
    if not enabled:
        return records

    def _has_effectiveness_fields(rec: Mapping[str, Any]) -> bool:
        keys = ("effective_status", *_EFFECTIVE_DATE_KEYS, *_EXPIRE_DATE_KEYS)
        return any(
            (rec or {}).get(key) not in (None, "") for key in keys
        )

    kept: list = []
    for record in records or []:
        try:
            if not isinstance(record, Mapping):
                kept.append(record)
                continue
            if not _has_effectiveness_fields(record):
                # 存量数据默认有效：无字段记录原样保留（不注入，防污染下游）
                kept.append(record)
                continue
            if is_in_force(record, now=now):
                kept.append(attach_effective_status(dict(record), record))
        except Exception:  # noqa: BLE001 - 单条判定异常不阻断整批
            kept.append(record)
    return kept
