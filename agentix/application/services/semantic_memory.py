"""R3-8 Semantic Memory 卡片层（架构文档 §29-33：Memory Architecture）。

四层记忆 ↔ 现有资产映射（§29 拆分要求的收口状态）::

    Working Memory   → AgentState / ExecutionStep（R3-1/R3-3，run 内生命周期）
    Episodic Memory  → MemorySummary（long_term_memory.py，会话摘要 + recall）
    Semantic Memory  → **本模块**（卡片式稳定信息 + 提取/去重/冲突管线）
    Procedural Memory→ 留待（见下方决策记录）

Semantic Memory（§32）管线要求：Memory Extractor → Importance → Confidence →
Dedup → Conflict Detection → Store。本模块 v1 实现全链路，但两处刻意从简：

- **Extractor 规则式**（零 LLM 调用）：从 Episodic 摘要文本中识别用户
  偏好/要求类句子；语义级抽取属后续演进（换实现不动存储协议）；
- **冲突判定字符级**（B1·M-P0-1 与画像卡统一）：同类型且内容互为包含
  → 视为同一事实的新版本：**只标记不覆写**（原内容保留，冲突值并入
  ``conflicts`` 行 ``{"key","values"}``，version+1 表示卡片修订）；
  完全一致 → 去重（confidence 取 max）；其余共存。

数据来源纪律（§32「不能把每一句对话都存进去」）：卡片**只从会话摘要
提取**（摘要本身已是 Episodic 压缩产物），不触碰原始对话。

Procedural Memory（§33）决策：当前无真实多步骤任务模板场景（KG 工作流
为静态构图），做空壳存储属 dead-code（P2-5 纪律）——待出现真实消费方
（如合同审查步骤模板）再建，接口不复用本模块。

接线：``LongTermMemory.store_summary`` 时提取并合并卡片（生产者）；
``get_context_for_query`` 时读回注入（消费者）；总开关
``MEMORY_CARDS_ENABLED``，且整体仍挂 ``MEMORY_CONTEXT_ENABLED`` 之下
（后者默认关 ⇒ 默认零行为变化）。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from agentix.agent.memory.service import redact_pii
from agentix.application.services.card_schema import (
    CARD_SCHEMA_VERSION,
    upsert_conflict_row,
)

# ---------------------------------------------------------------------------
# MemoryCard（§32 Store 侧协议）
# ---------------------------------------------------------------------------

CARD_TYPE_PREFERENCE = "preference"  # 用户偏好/要求（如"回答要附法条出处"）
CARD_TYPE_INSTRUCTION = "instruction"  # 操作性指示（如"先检索再回答"）

_CARD_TYPES = (CARD_TYPE_PREFERENCE, CARD_TYPE_INSTRUCTION)


@dataclass
class MemoryCard:
    """一条稳定语义记忆（§32：经 Extractor/Importance/Confidence/Dedup/Conflict 后才入库）。"""

    card_id: str
    card_type: str
    content: str
    importance: float = 0.5
    confidence: float = 0.6
    version: int = 1
    source: str = "summary_extraction"
    created_at: str = ""
    updated_at: str = ""
    # B1·M-P0-1（只增字段，旧序列化/旧数据不动）：与画像卡共用 schema 版本
    # 单源（card_schema.CARD_SCHEMA_VERSION）；冲突行与 fold_profile_card 同构。
    schema_version: int = CARD_SCHEMA_VERSION
    conflicts: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryCard":
        known = {f for f in cls.__dataclass_fields__}  # noqa: C416
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Memory Extractor（规则式 v1，零 LLM）
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT_RE = re.compile(r"[。！？；!?\n]+")

_PREFERENCE_HINT_RE = re.compile(r"(用户|客户).{0,8}(要求|偏好|希望|喜欢|习惯|倾向|强调)")
_NEGATION_RE = re.compile(r"(不要|禁止|别|不得)")
_INSTRUCTION_HINT_RE = re.compile(r"(回答|回复|输出|结论).{0,6}(要用|以|按|格式|风格|必须)")


def _normalize_card_content(text: str) -> str:
    """卡片内容归一化（去空白标点、小写）——去重/冲突判定的比较键。"""
    return re.sub(r"[\W_]+", "", text or "").lower()


def extract_cards(summary_text: str, *, min_chars: int = 6) -> List[MemoryCard]:
    """从 Episodic 摘要文本提取候选语义卡片（规则式 v1）。

    只抽「稳定信息」类句子（§32）：偏好/要求/指示；普通事实不进
    Semantic 层（那属于 Episodic 摘要本身的职责）。
    """
    if not summary_text or not summary_text.strip():
        return []
    cards: List[MemoryCard] = []
    now = _now_iso()
    for raw in _SENTENCE_SPLIT_RE.split(summary_text):
        text = raw.strip()
        if len(text) < min_chars:
            continue
        if _PREFERENCE_HINT_RE.search(text):
            card_type = CARD_TYPE_PREFERENCE
            importance = 0.8
        elif _INSTRUCTION_HINT_RE.search(text):
            card_type = CARD_TYPE_INSTRUCTION
            importance = 0.7
        else:
            continue
        cards.append(
            MemoryCard(
                card_id=uuid.uuid4().hex[:12],
                card_type=card_type,
                content=text,
                importance=importance,
                confidence=0.6,  # 规则式抽取固定置信度（语义级抽取时再分级）
                created_at=now,
                updated_at=now,
            )
        )
    return cards


# ---------------------------------------------------------------------------
# Dedup + Conflict Detection（§32 管线中段，纯函数核心）
# ---------------------------------------------------------------------------

@dataclass
class MergeReport:
    """merge_cards 结果统计（观测/日志用）。"""

    added: int = 0
    deduped: int = 0
    conflicts: int = 0
    unchanged: int = 0

    def to_dict(self) -> Dict[str, int]:
        return asdict(self)


def merge_cards(
    existing: List[MemoryCard], incoming: List[MemoryCard]
) -> Tuple[List[MemoryCard], MergeReport]:
    """把 incoming 卡片合并进 existing（纯函数，不改输入对象）。

    判定顺序（对每张 incoming 卡）：
    1. **Dedup**：同类型且归一化内容完全一致 → 跳过，existing 置信度取 max；
    2. **Conflict**：同类型且内容互为包含（子串关系）→ 同一事实的新旧两版：
       B1·M-P0-1 **只标记不覆写**——原内容保留在 ``content``，冲突值并入
       ``conflicts`` 行（``{"key","values"}``，与 fold_profile_card 同构），
       version+1 表示卡片修订，updated_at 刷新；
    3. 其余 → 新增共存。
    """
    merged: List[MemoryCard] = list(existing)
    report = MergeReport()

    for card in incoming:
        norm_in = _normalize_card_content(card.content)
        if not norm_in:
            continue
        target_idx: Optional[int] = None
        status = "add"
        for i, old in enumerate(merged):
            if old.card_type != card.card_type:
                continue
            norm_old = _normalize_card_content(old.content)
            if norm_in == norm_old:
                status = "dedup"
                target_idx = i
                break
            if norm_in in norm_old or norm_old in norm_in:
                status = "conflict"
                target_idx = i
                break
        if status == "dedup" and target_idx is not None:
            old = merged[target_idx]
            merged[target_idx] = MemoryCard(
                **{**old.to_dict(), "confidence": max(old.confidence, card.confidence)}
            )
            report.deduped += 1
        elif status == "conflict" and target_idx is not None:
            old = merged[target_idx]
            # B1·M-P0-1：同键冲突**只标记不覆写**（与 fold_profile_card 对齐）——
            # old.content 保留，新旧两值进同一 conflicts 行（helper 去重排序）。
            merged[target_idx] = MemoryCard(
                **{
                    **old.to_dict(),
                    "conflicts": upsert_conflict_row(
                        list(old.conflicts or []),
                        _normalize_card_content(old.content),
                        [old.content, card.content],
                    ),
                    "version": old.version + 1,
                    "updated_at": _now_iso(),
                }
            )
            report.conflicts += 1
        else:
            merged.append(card)
            report.added += 1

    return merged, report


def dict_to_card(data: Dict[str, Any]) -> MemoryCard:
    return MemoryCard.from_dict(data)


def _redacted(card: MemoryCard) -> MemoryCard:
    """B1·M-P0-3：卡片内容脱敏副本（幂等——已脱敏文本不再命中任何规则）。"""
    content = redact_pii(card.content)
    if content == card.content:
        return card
    return MemoryCard(**{**card.to_dict(), "content": content})


# ---------------------------------------------------------------------------
# SemanticMemoryStore（Redis hash 存储）
# ---------------------------------------------------------------------------

class SemanticMemoryStore:
    """语义卡片存储（Redis hash：field=card_id, value=json）。

    依赖注入 ``redis_client``（测试用 fake），与 LongTermMemory 同
    prefix 空间（``{prefix}:cards:{user_id}``）。
    """

    def __init__(self, *, redis_client: Any, prefix: str = "ltm") -> None:
        self._redis = redis_client
        self._prefix = prefix

    def _key(self, user_id: str) -> str:
        return f"{self._prefix}:cards:{user_id}"

    async def store_cards(self, user_id: str, incoming: List[MemoryCard]) -> MergeReport:
        """提取结果合并入库（Dedup/Conflict 语义见 :func:`merge_cards`）。

        B1·M-P0-3（写路径单点覆盖，生产者与直调方一起罩住）：入库前对
        incoming 与存量卡片内容做 ``redact_pii``——Redis 中不存 PII 原文；
        legacy 原文卡片**重写即清洗**（与存储不一致 ⇒ 必写回）。
        """
        if not incoming:
            return MergeReport()
        stored = await self.recall_cards(user_id, limit=500)
        incoming = [_redacted(c) for c in incoming]
        existing = [_redacted(c) for c in stored]
        merged, report = merge_cards(existing, incoming)
        if merged == stored:
            return report  # 与存储逐位一致才免写（脱敏导致的差异 ⇒ 写回清洗）
        mapping = {c.card_id: _dumps(c) for c in merged}
        await self._redis.delete(self._key(user_id))
        if mapping:
            await self._redis.hset(self._key(user_id), mapping=mapping)
        logger.info(
            "semantic memory merged: user={} added={} deduped={} conflicts={}",
            user_id,
            report.added,
            report.deduped,
            report.conflicts,
        )
        return report

    async def recall_cards(self, user_id: str, *, limit: int = 10) -> List[MemoryCard]:
        raw = await self._redis.hgetall(self._key(user_id))
        if not raw:
            return []
        cards = [MemoryCard.from_dict(_loads(v)) for v in raw.values()]
        # importance 降序为主，同分时 updated_at 新者优先（两次稳定排序）
        cards.sort(key=lambda c: c.updated_at, reverse=True)
        cards.sort(key=lambda c: -c.importance)
        return cards[:limit]

    async def clear_cards(self, user_id: str) -> int:
        count = await self._redis.delete(self._key(user_id))
        return int(count or 0)


def _dumps(card: MemoryCard) -> str:
    import json

    return json.dumps(card.to_dict(), ensure_ascii=False)


def _loads(raw: str) -> Dict[str, Any]:
    import json

    return json.loads(raw)
