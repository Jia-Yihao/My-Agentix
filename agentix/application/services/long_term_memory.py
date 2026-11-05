"""
Long-term memory system for cross-session knowledge accumulation.

Stores:
- Per-user conversation summaries (auto-generated after each session)
- User preferences and frequently asked topics
- Key facts extracted from conversations
- Semantic search over historical summaries

Architecture:
  Short-term memory: LangGraph Checkpointer (current session)
  Long-term memory:  Redis + optional vector store (cross-session)
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from loguru import logger
from redis.asyncio import Redis

from agentix.agent.memory.service import redact_pii
from agentix.application.services.card_schema import (
    CARD_SCHEMA_VERSION,
    canonical_json,
    upsert_conflict_row,
)
from agentix.application.services.profile_scope import (
    project_profile_card,
    project_profile_topics,
)
from agentix.config import settings


@dataclass
class MemorySummary:
    """A summary of a conversation session."""
    session_id: str
    user_id: str
    summary: str
    topics: List[str] = field(default_factory=list)
    key_facts: List[str] = field(default_factory=list)
    message_count: int = 0
    created_at: float = field(default_factory=time.time)
    access_count: int = 0
    last_accessed: float = field(default_factory=time.time)
    # 批次 10（§10.10.2）：可重建判据溯源字段——本次摘要覆盖的原始消息 id
    # （与摘要窗口一致，默认 messages[-20:]）。LLM 摘要是不可逆变换：
    # 在覆盖验证通过前，被覆盖的原始消息不得物理删除。当前**只记录不阻断**。
    source_message_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "summary": self.summary,
            "topics": self.topics,
            "key_facts": self.key_facts,
            "message_count": self.message_count,
            "created_at": self.created_at,
            "access_count": self.access_count,
            "last_accessed": self.last_accessed,
            "source_message_ids": self.source_message_ids,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemorySummary":
        return cls(
            session_id=data["session_id"],
            user_id=data["user_id"],
            summary=data["summary"],
            topics=data.get("topics", []),
            key_facts=data.get("key_facts", []),
            message_count=data.get("message_count", 0),
            created_at=data.get("created_at", time.time()),
            access_count=data.get("access_count", 0),
            last_accessed=data.get("last_accessed", time.time()),
            source_message_ids=data.get("source_message_ids", []),
        )


def is_rebuildable(items: List[Dict[str, Any]], summary: MemorySummary) -> bool:
    """可重建压缩判据（批次 10，§10.10.2 纯函数先行）。

    判定：待淘汰的原始消息 ``items`` 是否**全部**被某条摘要溯源覆盖——
    即 ``set(items.ids) ⊆ set(summary.source_message_ids)``。成立则理论上
    可由摘要覆盖证明淘汰安全；不成立则原始消息必须保留。

    **空真防护**（吸取 evals 空真地板教训）：``items`` 为空、或所有消息都
    没有 id 时返回 ``False``——「无证据」不等于「已覆盖」，缺 id 的消息
    永远不可判定为可重建（保守默认）。

    演进顺序：当前**只记录不阻断**（source_message_ids 为审计字段）；待
    覆盖率数据足够，再决定是否把本判据接进淘汰路径
    （``max_summaries_per_user`` 淘汰、窗口裁剪）。
    """
    item_ids = {
        str(m["id"]) for m in items if isinstance(m, dict) and m.get("id") is not None
    }
    if not item_ids:
        return False
    return item_ids <= set(summary.source_message_ids)


@dataclass
class UserProfile:
    """Aggregated user profile built from historical interactions."""
    user_id: str
    total_sessions: int = 0
    total_messages: int = 0
    frequent_topics: List[str] = field(default_factory=list)
    preferences: Dict[str, Any] = field(default_factory=dict)
    last_active: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)
    # Phase 29（Ch3 §3.5）：结构化画像卡——只增字段，旧键不动。
    # 序列化仅在 MEMORY_PROFILE_CARD_ENABLED 开时写入（flag-off 字节等价）；
    # 读侧 from_dict 恒读（旧数据无卡 ⇒ None，回退旧字段）。
    profile_card: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "user_id": self.user_id,
            "total_sessions": self.total_sessions,
            "total_messages": self.total_messages,
            "frequent_topics": self.frequent_topics,
            "preferences": self.preferences,
            "last_active": self.last_active,
            "created_at": self.created_at,
        }
        if settings.MEMORY_PROFILE_CARD_ENABLED and self.profile_card is not None:
            data["profile_card"] = self.profile_card
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UserProfile":
        return cls(
            user_id=data["user_id"],
            total_sessions=data.get("total_sessions", 0),
            total_messages=data.get("total_messages", 0),
            frequent_topics=data.get("frequent_topics", []),
            preferences=data.get("preferences", {}),
            last_active=data.get("last_active", time.time()),
            created_at=data.get("created_at", time.time()),
            profile_card=data.get("profile_card"),
        )


# ---------------------------------------------------------------------------
# Phase 29（Ch3 §3.5/§3.6）：JSON Cards + User as Code
#
# `profile_card` = 稳定、可校验的结构化画像（卡片轨）；叙事情节仍走
# MemorySummary/Simple Notes（笔记轨）——双轨并存，弃一即违反 §3.5。
#
# `fold_profile_card` 是增量维护与确定性重放**共用的唯一核心**（单事件折入），
# `derive_profile` = 排序事件日志后顺序折入 ⇒ 派生幂等（replay ≡ 增量）。
# 纯函数、零第三方、零 I/O。
# ---------------------------------------------------------------------------

#: 卡片结构版本（字段结构变化时 +1；读侧对未知版本原样保留，不猜不迁移）。
#: B1·M-P0-1：画像卡与语义卡（``MemoryCard.schema_version``）**单源**共用
#: ``card_schema.CARD_SCHEMA_VERSION``——双卡合并的版本号不再各写各的。
PROFILE_CARD_SCHEMA_VERSION = CARD_SCHEMA_VERSION

#: legal_domain_prefs 保留的领域数（按出现次数降序截断——卡片喂 prompt 需紧凑）
PROFILE_CARD_DOMAIN_CAP = 20


def _empty_profile_card() -> Dict[str, Any]:
    return {
        "schema_version": PROFILE_CARD_SCHEMA_VERSION,
        "legal_domain_prefs": [],
        "usage_habits": {
            "sessions": 0,
            "messages": 0,
            "avg_messages_per_session": 0.0,
            "first_active": None,
            "last_active": None,
        },
        "user_constraints": {},
        "conflicts": [],
    }


def _sorted_domain_prefs(counts: Dict[str, int]) -> List[Dict[str, Any]]:
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [
        {"topic": topic, "count": count}
        for topic, count in ranked[:PROFILE_CARD_DOMAIN_CAP]
    ]


def fold_profile_card(
    card: Optional[Dict[str, Any]], event: Dict[str, Any]
) -> Dict[str, Any]:
    """把一条会话事件折入 ``profile_card``（**不修改输入**，返回新卡片）。

    事件 schema（白名单，其余键忽略）——即 ``MemorySummary.to_dict()`` 形状：

    - ``created_at``（float）→ usage_habits 的 first/last_active（min/max）
    - ``message_count``（int）→ usage_habits.messages 累加
    - ``topics``（list[str]，事件内去重）→ legal_domain_prefs 计数
    - ``preferences``（dict，**可选**）→ user_constraints 观测来源：
      单一稳定值进 ``user_constraints``；同键出现不同值 ⇒ **只标记
      conflicts、不覆写**原值（Ch3 §3.6 确定性约束检查）。

    计数/极值/去重集合均满足交换律 ⇒ 增量折入与 ``derive_profile``
    重放折同一事件序列结果一致（幂等，见 DoD）。``conflicts``/领域列表
    每次折叠后统一排序 ⇒ 与输入次序无关。
    """
    base = (
        card
        if isinstance(card, dict)
        and card.get("schema_version") == PROFILE_CARD_SCHEMA_VERSION
        else _empty_profile_card()
    )
    usage = dict(base.get("usage_habits") or {})
    domain_counts = {
        str(row.get("topic")): int(row.get("count") or 0)
        for row in (base.get("legal_domain_prefs") or [])
        if isinstance(row, dict)
    }
    constraints = dict(base.get("user_constraints") or {})
    conflicts: List[Dict[str, Any]] = [
        {"key": str(row.get("key")), "values": list(row.get("values") or [])}
        for row in (base.get("conflicts") or [])
        if isinstance(row, dict)
    ]

    # -- usage_habits ------------------------------------------------------
    usage["sessions"] = int(usage.get("sessions") or 0) + 1
    message_count = event.get("message_count")
    if isinstance(message_count, (int, float)) and not isinstance(message_count, bool):
        usage["messages"] = int(usage.get("messages") or 0) + int(message_count)
    ts = event.get("created_at")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        ts = float(ts)
        first = usage.get("first_active")
        last = usage.get("last_active")
        usage["first_active"] = ts if first is None else min(float(first), ts)
        usage["last_active"] = ts if last is None else max(float(last), ts)

    # -- legal_domain_prefs ------------------------------------------------
    topics = event.get("topics")
    if isinstance(topics, list):
        for topic in {t for t in topics if isinstance(t, str)}:
            domain_counts[topic] = domain_counts.get(topic, 0) + 1

    # -- user_constraints / conflicts（只标记不覆写；B1·M-P0-1 统一冲突行）---
    prefs = event.get("preferences")
    if isinstance(prefs, dict):
        for raw_key, value in prefs.items():
            if not isinstance(raw_key, str):
                continue
            canon = canonical_json(value)
            conflict_row = next(
                (row for row in conflicts if row["key"] == raw_key), None
            )
            if conflict_row is not None:
                # 同键冲突行已存在：新观测值并入同一行（helper 负责去重排序）
                conflicts = upsert_conflict_row(conflicts, raw_key, [value])
                continue
            if raw_key in constraints:
                if canonical_json(constraints[raw_key]) != canon:
                    # 冲突：原值保留在 user_constraints（不覆写），并标记
                    conflicts = upsert_conflict_row(
                        conflicts, raw_key, [constraints[raw_key], value]
                    )
                continue
            constraints[raw_key] = value

    sessions = int(usage.get("sessions") or 0)
    messages = int(usage.get("messages") or 0)
    conflicts.sort(key=lambda row: row["key"])
    return {
        "schema_version": PROFILE_CARD_SCHEMA_VERSION,
        "legal_domain_prefs": _sorted_domain_prefs(domain_counts),
        "usage_habits": {
            "sessions": sessions,
            "messages": messages,
            "avg_messages_per_session": round(messages / sessions, 2)
            if sessions
            else 0.0,
            "first_active": usage.get("first_active"),
            "last_active": usage.get("last_active"),
        },
        "user_constraints": constraints,
        "conflicts": conflicts,
    }


def derive_profile(
    events: List[Dict[str, Any]], *, user_id: str = "default_user"
) -> UserProfile:
    """User as Code（Ch3 §3.6）：从会话事件日志**确定性重派生**用户画像。

    纯函数：事件按 ``(created_at, session_id)`` 排序后顺序折入
    :func:`fold_profile_card`，并同步重算旧增量字段（``total_sessions`` /
    ``total_messages`` / ``frequent_topics`` 首见序、容量 50 —— 与
    ``_update_user_profile`` 增量规则一致）。``last_active``/``created_at``
    取事件时间极值（无事件时间 ⇒ 0.0，保持确定性、不引 ``time.time()``）。

    **不覆写**语义：返回的新画像只供调用方按需取 ``profile_card``；是否
    落盘由调用方（``refresh_profile_card``，触发器路径）决定，读路径不隐式重放。

    :param events: 会话事件记录（``MemorySummary.to_dict()`` 形状，可选
        ``preferences`` 键）；非 dict 项忽略。
    :return: 派生画像；**无有效事件时 ``profile_card`` 为 None**（不伪造空卡）。
    """
    ordered = [e for e in events if isinstance(e, dict)]
    ordered.sort(
        key=lambda e: (
            float(e["created_at"])
            if isinstance(e.get("created_at"), (int, float))
            and not isinstance(e.get("created_at"), bool)
            else float("-inf"),
            str(e.get("session_id", "")),
        )
    )
    if not ordered:
        return UserProfile(user_id=user_id, profile_card=None)

    card: Optional[Dict[str, Any]] = None
    total_messages = 0
    frequent_topics: List[str] = []
    timestamps: List[float] = []
    for event in ordered:
        card = fold_profile_card(card, event)
        message_count = event.get("message_count")
        if isinstance(message_count, (int, float)) and not isinstance(
            message_count, bool
        ):
            total_messages += int(message_count)
        topics = event.get("topics")
        if isinstance(topics, list):
            for topic in topics:
                if isinstance(topic, str) and topic not in frequent_topics:
                    frequent_topics.append(topic)
        ts = event.get("created_at")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            timestamps.append(float(ts))

    return UserProfile(
        user_id=user_id,
        total_sessions=len(ordered),
        total_messages=total_messages,
        frequent_topics=frequent_topics[-50:],
        last_active=max(timestamps) if timestamps else 0.0,
        created_at=min(timestamps) if timestamps else 0.0,
        profile_card=card,
    )


class LongTermMemory:
    """Cross-session long-term memory backed by Redis."""

    def __init__(
        self,
        *,
        redis_client: Optional[Redis] = None,
        redis_url: Optional[str] = None,
        prefix: str = "ltm",
        summary_ttl: int = 86400 * 90,
        max_summaries_per_user: int = 200,
    ):
        self._redis = redis_client or Redis.from_url(
            redis_url or settings.REDIS_URL,
            decode_responses=True,
        )
        self.prefix = prefix
        self.summary_ttl = summary_ttl
        self.max_summaries_per_user = max_summaries_per_user

    async def store_summary(
        self,
        session_id: str,
        summary: str,
        messages: List[Dict[str, Any]],
        *,
        user_id: str = "default_user",
        topics: Optional[List[str]] = None,
        key_facts: Optional[List[str]] = None,
        source_message_ids: Optional[List[str]] = None,
    ) -> MemorySummary:
        """Store a conversation summary for long-term recall.

        ``source_message_ids``：被摘要覆盖的原始消息 id（§10.10.2 溯源）。
        缺省时按当前摘要窗口（最后 20 条）自行提取带 id 的消息——调用方
        （task_handlers）应显式传入与摘要窗口一致的 id 列表，避免窗口漂移
        导致溯源失真。无 id 的消息无法溯源（保守：不入列表）。
        """
        if source_message_ids is None:
            source_message_ids = [
                str(m["id"])
                for m in messages[-20:]
                if isinstance(m, dict) and m.get("id") is not None
            ]
        # B1·M-P0-3（写路径单点覆盖）：正文在落盘前脱敏——Redis 中不存 PII 原文，
        # 派生卡片（extract_cards）读的也是脱敏后文本。幂等：已脱敏文本不再命中。
        summary = redact_pii(summary)
        topics = [redact_pii(t) for t in (topics or [])]
        key_facts = [redact_pii(k) for k in (key_facts or [])]
        mem = MemorySummary(
            session_id=session_id,
            user_id=user_id,
            summary=summary,
            topics=topics,
            key_facts=key_facts,
            message_count=len(messages),
            source_message_ids=list(source_message_ids),
        )

        summary_key = f"{self.prefix}:summary:{session_id}"
        await self._redis.set(
            summary_key,
            json.dumps(mem.to_dict()),
            ex=self.summary_ttl,
        )

        user_index_key = f"{self.prefix}:user:{user_id}:sessions"
        await self._redis.zadd(
            user_index_key,
            {session_id: mem.created_at},
        )
        await self._redis.zremrangebyrank(
            user_index_key,
            0,
            -(self.max_summaries_per_user + 1),
        )
        await self._redis.expire(user_index_key, self.summary_ttl)

        await self._update_user_profile(user_id, mem)

        # R3-8 Semantic Memory（架构文档 §32）：摘要入库时提取语义卡片
        # （Extractor→Dedup→Conflict→Store 管线）；开关关闭或提取异常均不影响摘要主链路。
        if getattr(settings, "MEMORY_CARDS_ENABLED", False):
            try:
                from agentix.application.services.semantic_memory import (
                    SemanticMemoryStore,
                    extract_cards,
                )

                cards = extract_cards(summary)
                if cards:
                    store = SemanticMemoryStore(redis_client=self._redis, prefix=self.prefix)
                    await store.store_cards(user_id, cards)
            except Exception as exc:  # noqa: BLE001 - 卡片提取失败不阻断摘要存储
                logger.warning("semantic card extraction failed (non-fatal): {}", exc)

        logger.info(
            "Long-term memory stored: session={} user={} summary_len={}",
            session_id,
            user_id,
            len(summary),
        )
        return mem

    async def recall(
        self,
        user_id: str = "default_user",
        *,
        limit: int = 5,
        query: Optional[str] = None,
    ) -> List[MemorySummary]:
        """Recall relevant past summaries for a user.

        Args:
            user_id: The user to recall memories for.
            limit: Maximum number of summaries to return.
            query: Optional query text for relevance filtering.
        """
        user_index_key = f"{self.prefix}:user:{user_id}:sessions"
        session_ids = await self._redis.zrevrange(user_index_key, 0, limit * 3 - 1)

        if not session_ids:
            return []

        summaries: List[MemorySummary] = []
        for session_id in session_ids:
            raw = await self._redis.get(f"{self.prefix}:summary:{session_id}")
            if not raw:
                continue
            try:
                mem = MemorySummary.from_dict(json.loads(raw))
                summaries.append(mem)
            except (json.JSONDecodeError, KeyError):
                logger.warning("Failed to decode memory for session {}", session_id)

        if query and summaries:
            query_lower = query.lower()
            # 批次 7：相关性分 = 关键词命中数 × 时效衰减因子。
            # 旧行为只有关键词重合（score += 1），旧摘要与新摘要同分时无法
            # 体现「近期的记忆更相关」。半衰期可在 settings 关闭（<=0 ⇒ 因子恒 1，
            # 行为与旧版逐位一致）。
            half_life = settings.MEMORY_RECENCY_HALF_LIFE_DAYS
            now = time.time()
            scored = []
            for mem in summaries:
                keyword_hits = 0
                text = (mem.summary + " " + " ".join(mem.topics)).lower()
                for word in query_lower.split():
                    if word in text:
                        keyword_hits += 1
                if half_life > 0:
                    age_days = max(0.0, (now - mem.created_at) / 86400.0)
                    recency_factor = 0.5 ** (age_days / half_life)
                else:
                    recency_factor = 1.0
                scored.append((keyword_hits * recency_factor, mem.created_at, mem))
            # 分数降序；同分时新摘要优先（可预期的确定性次序）
            scored.sort(key=lambda x: (-x[0], -x[1]))
            summaries = [mem for _, _, mem in scored[:limit]]
        else:
            summaries = summaries[:limit]

        for mem in summaries:
            mem.access_count += 1
            mem.last_accessed = time.time()
            await self._redis.set(
                f"{self.prefix}:summary:{mem.session_id}",
                json.dumps(mem.to_dict()),
                ex=self.summary_ttl,
            )

        return summaries

    async def get_user_profile(self, user_id: str = "default_user") -> Optional[UserProfile]:
        """Get the aggregated user profile."""
        raw = await self._redis.get(f"{self.prefix}:profile:{user_id}")
        if not raw:
            return None
        try:
            return UserProfile.from_dict(json.loads(raw))
        except (json.JSONDecodeError, KeyError):
            return None

    async def _update_user_profile(self, user_id: str, new_summary: MemorySummary) -> None:
        """Update user profile with new summary data."""
        profile = await self.get_user_profile(user_id)
        if profile is None:
            profile = UserProfile(user_id=user_id)

        profile.total_sessions += 1
        profile.total_messages += new_summary.message_count
        profile.last_active = time.time()

        for topic in new_summary.topics:
            if topic not in profile.frequent_topics:
                profile.frequent_topics.append(topic)
        profile.frequent_topics = profile.frequent_topics[-50:]

        # Phase 29：flag 开时把本次事件折入 profile_card（增量路径与
        # derive_profile 重放共用 fold_profile_card ⇒ 幂等）。flag-off 零改动。
        if settings.MEMORY_PROFILE_CARD_ENABLED:
            profile.profile_card = fold_profile_card(
                profile.profile_card, new_summary.to_dict()
            )

        await self._redis.set(
            f"{self.prefix}:profile:{user_id}",
            json.dumps(profile.to_dict()),
            ex=self.summary_ttl,
        )

    async def refresh_profile_card(
        self, user_id: str = "default_user"
    ) -> Optional[Dict[str, Any]]:
        """User as Code 重派生（Phase 29）：从**当前**会话事件日志重算 ``profile_card``。

        - **只在触发器路径调用**（``memory_consolidation`` handler、flag 开）；
          读取时**不**隐式重放（Ch3 §3.6：读路径零成本）。
        - 只更新 ``profile_card``——不动既有计数器/``created_at`` 等增量字段
          （只增不改原则 3；合并/淘汰后事件日志与历史增量可能不一致，卡片
          以事件日志为准，旧字段保持其既有语义）。
        - 事件日志为空 ⇒ 返回 ``None`` 且**不写**（不以空卡覆盖旧卡）。
        - flag-off ⇒ 返回 ``None`` 零存储触碰（防御：即使被直接调用）。

        :return: 派生出的卡片；无事件/flag-off 时 ``None``。
        """
        if not settings.MEMORY_PROFILE_CARD_ENABLED:
            return None
        events = [s.to_dict() for s in await self._load_all_summaries(user_id)]
        derived = derive_profile(events, user_id=user_id)
        if derived.profile_card is None:
            return None
        profile = await self.get_user_profile(user_id)
        if profile is None:
            profile = UserProfile(user_id=user_id)
        profile.profile_card = derived.profile_card
        await self._redis.set(
            f"{self.prefix}:profile:{user_id}",
            json.dumps(profile.to_dict()),
            ex=self.summary_ttl,
        )
        return derived.profile_card

    # ------------------------------------------------------------------
    # F2a·M-P2-4（memory-optimization §6 画像作用域）：跨会话沿用授权边界
    # + 一键清除全链路。授权状态单键存储（``{prefix}:profile_scope:{user}``，
    # 值 ``granted`` = 授权）；读取 fail-closed（无授权证据 ⇒ 不沿用）。
    # ------------------------------------------------------------------

    def _profile_scope_key(self, user_id: str) -> str:
        return f"{self.prefix}:profile_scope:{user_id}"

    async def profile_scope_consent(self, user_id: str = "default_user") -> bool:
        """读取跨会话沿用授权：仅 ``granted`` 视为授权；**读取异常 fail-closed**
        （授权边界不因存储故障放开）。
        """
        try:
            raw = await self._redis.get(self._profile_scope_key(user_id))
        except Exception as exc:  # noqa: BLE001 - 授权读取故障 ⇒ 无授权证据
            logger.warning("profile scope consent read degraded (fail-closed): {}", exc)
            return False
        return raw == "granted"

    async def set_profile_scope_consent(
        self, user_id: str = "default_user", *, granted: bool
    ) -> None:
        """写入/撤销跨会话沿用授权（审计留痕：授权状态变化必落日志）。"""
        key = self._profile_scope_key(user_id)
        if granted:
            await self._redis.set(key, "granted")
            logger.info("profile scope consent granted: user={}", user_id)
        else:
            await self._redis.delete(key)
            logger.info("profile scope consent revoked: user={}", user_id)

    # ------------------------------------------------------------------
    # 整理（consolidation）：遗忘与合并的**执行侧**
    #
    # 判定逻辑（分组 / 合并 / 淘汰计划）都在 `memory_consolidation.py` 里做成纯函数；
    # 这里只负责「读出来 → 交给纯函数 → 把结论写回去」。
    # 这样拆的理由：判定规则可离线单测、可评审，I/O 才留在本类。
    # ------------------------------------------------------------------

    async def try_claim_consolidation(
        self,
        user_id: str = "default_user",
        *,
        window_seconds: int = 86400,
        force: bool = False,
    ) -> bool:
        """整理幂等闸：``SET NX EX`` 声明本轮整理权（窗口内重复触发返回 False）。

        ``force=True`` 无条件声明并刷新窗口；``window_seconds <= 0`` 关闭
        窗口（总是可触发）。声明失败 = 窗口内已有成功触发，调用方应跳过。
        """
        if window_seconds <= 0 or force:
            await self._redis.set(
                f"{self.prefix}:consolidation:last:{user_id}",
                str(time.time()),
                ex=max(1, int(window_seconds)),
            )
            return True
        claimed = await self._redis.set(
            f"{self.prefix}:consolidation:last:{user_id}",
            str(time.time()),
            ex=int(window_seconds),
            nx=True,
        )
        return bool(claimed)

    async def consolidate(
        self,
        user_id: str = "default_user",
        *,
        max_gap_days: Optional[float] = None,
        max_groups: Optional[int] = None,
        ttl_days: Optional[float] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """整理该用户的长期记忆：**合并**相关摘要 + **淘汰**过期/超容摘要。

        步骤（全部确定性，无 LLM 调用）：

        1. 读出全部摘要；
        2. :func:`select_consolidation_groups` 选出「同主题 + 时间相邻」的组；
        3. 每组 :func:`merge_summaries` 合并为一条（topics/key_facts 去重并集、
           ``source_message_ids`` **并集**——这是可重建判据在整理后仍成立的前提）；
        4. :func:`plan_summary_eviction` 规划 TTL / 容量淘汰；
        5. 写回：**被淘汰的摘要先归档正文再删原键**（淘汰红线：摘要保留 +
           证据可回溯，禁止静默销毁），被合并掉的条目正文已完整并入合并结果
           （并集语义）可直接删除，最后修正索引。

        :param dry_run: ``True`` 时只返回计划，**不写任何数据**（便于先观察再执行——
            整理会改写历史，默认建议先 dry-run）。
        :return: 整理报告（组数 / 合并减少条数 / 淘汰条数 / 保留条数）。
        """
        from agentix.application.services.memory_consolidation import (
            merge_summaries,
            plan_summary_eviction,
            select_consolidation_groups,
        )

        gap = (
            float(settings.MEMORY_CONSOLIDATION_MAX_GAP_DAYS)
            if max_gap_days is None
            else float(max_gap_days)
        )
        groups_limit = (
            int(settings.MEMORY_CONSOLIDATION_MAX_GROUPS)
            if max_groups is None
            else int(max_groups)
        )
        ttl = (self.summary_ttl / 86400.0) if ttl_days is None else float(ttl_days)

        summaries = await self._load_all_summaries(user_id)
        if not summaries:
            return {
                "user_id": user_id,
                "before": 0,
                "after": 0,
                "groups": 0,
                "merged_away": 0,
                "evicted": 0,
                "dry_run": dry_run,
                "merged_session_ids": [],
                "evicted_session_ids": [],
                "archived_session_ids": [],
            }

        plan = select_consolidation_groups(
            summaries, now=time.time(), max_gap_days=gap, max_groups=groups_limit
        )

        merged: List[MemorySummary] = []
        merged_away_ids: set = set()
        for group in plan.groups:
            combined = merge_summaries(group)
            merged.append(combined)
            for member in group:
                # 合并结果的 session_id 沿用组内最早的会话；其余视为「被合并掉」
                if member.session_id != combined.session_id:
                    merged_away_ids.add(member.session_id)

        untouched = [s for s in summaries if s.session_id not in merged_away_ids]
        # 未参与合并的原条目 + 合并后的条目 → 一起进入淘汰规划
        candidates = [s for s in untouched if s.session_id not in {m.session_id for m in merged}]
        pool = candidates + merged

        keep, evict = plan_summary_eviction(
            pool,
            now=time.time(),
            ttl_days=ttl,
            max_items=self.max_summaries_per_user,
            half_life_days=float(settings.MEMORY_RECENCY_HALF_LIFE_DAYS),
        )
        evicted_ids = {s.session_id for s in evict}

        report = {
            "user_id": user_id,
            "before": len(summaries),
            "after": len(keep),
            "groups": len(plan.groups),
            "merged_away": len(merged_away_ids),
            "evicted": len(evict),
            "dry_run": dry_run,
            "merged_session_ids": sorted(m.session_id for m in merged),
            "evicted_session_ids": sorted(evicted_ids),
            "archived_session_ids": sorted(evicted_ids),
        }
        if dry_run:
            return report

        user_index_key = f"{self.prefix}:user:{user_id}:sessions"
        for combined in merged:
            await self._redis.set(
                f"{self.prefix}:summary:{combined.session_id}",
                json.dumps(combined.to_dict()),
                ex=self.summary_ttl,
            )

        # 淘汰红线：被淘汰的摘要**先归档正文**（含 source_message_ids 证据链）
        # 再删原键——归档键与正文同 TTL，report.archived_session_ids 可回溯。
        pool_by_id = {s.session_id: s for s in pool}
        for session_id in sorted(evicted_ids):
            original = pool_by_id.get(session_id)
            if original is not None:
                await self._redis.set(
                    f"{self.prefix}:archived:summary:{session_id}",
                    json.dumps(original.to_dict()),
                    ex=self.summary_ttl,
                )

        # 被合并掉 / 被淘汰的条目：删除正文（淘汰者已归档，合并者内容已并入
        # 合并结果）；索引稍后整体重建，此处不逐个摘除
        for session_id in merged_away_ids | evicted_ids:
            await self._redis.delete(f"{self.prefix}:summary:{session_id}")

        # 用「保留集」重建索引（整体重建比逐个摘除更不易出错）
        await self._redis.delete(user_index_key)
        for entry in keep:
            await self._redis.zadd(user_index_key, {entry.session_id: entry.created_at})
        await self._redis.expire(user_index_key, self.summary_ttl)

        logger.info(
            "Memory consolidated: user={} before={} after={} groups={} evicted={}",
            user_id,
            len(summaries),
            len(keep),
            len(plan.groups),
            len(evict),
        )
        return report

    async def _load_all_summaries(self, user_id: str) -> List[MemorySummary]:
        """读出该用户的全部摘要（按时间升序）。"""
        user_index_key = f"{self.prefix}:user:{user_id}:sessions"
        session_ids = await self._redis.zrange(user_index_key, 0, -1)
        out: List[MemorySummary] = []
        for session_id in session_ids:
            raw = await self._redis.get(f"{self.prefix}:summary:{session_id}")
            if not raw:
                continue
            try:
                out.append(MemorySummary.from_dict(json.loads(raw)))
            except (json.JSONDecodeError, KeyError):
                logger.warning("Failed to decode memory for session {}", session_id)
        out.sort(key=lambda s: s.created_at)
        return out

    async def get_context_for_query(
        self,
        query: str,
        user_id: str = "default_user",
        *,
        max_summaries: int = 3,
    ) -> str:
        """Build a context string from long-term memory for injection into prompts.

        Returns a formatted string that can be appended to system prompts.

        P6-6.4（§5 表 6.4 fail-open 检索容错）：摘要段与画像段**各自**兜底——
        任一段失败只丢该段（记 warning），其余段照常拼装（部分存活）；
        卡片区本就 fail-open（``_cards_context_or_empty``）。
        """
        try:
            summaries = await self.recall(user_id, limit=max_summaries, query=query)
        except Exception as exc:  # noqa: BLE001 - 单段失败不炸整段上下文
            logger.warning("memory summaries recall degraded (non-fatal): {}", exc)
            summaries = []
        try:
            profile = await self.get_user_profile(user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory profile load degraded (non-fatal): {}", exc)
            profile = None

        # F2a·M-P2-4 画像作用域：flag-on 时先取跨会话沿用授权（fail-closed：
        # 读取异常 ⇒ 无授权 ⇒ 稳定层字段扣下）；flag-off ⇒ allow_stable 恒
        # True，下面的投影为直通拷贝，注入与基线逐位等价。
        allow_stable = True
        if settings.MEMORY_PROFILE_SCOPE_ENABLED:
            allow_stable = await self.profile_scope_consent(user_id)

        if not summaries and not profile:
            # R3-8：无摘要/画像时仍可能存在语义卡片，不提前 return
            cards_part = await self._cards_context_or_empty(user_id)
            if cards_part and not allow_stable:
                # 语义卡 = 稳定层 ⇒ 未授权不跨会话注入（flag-on 才可达）
                logger.info(
                    "profile scope: semantic cards withheld (no cross-session consent) user={}",
                    user_id,
                )
                return ""
            return cards_part

        parts: List[str] = []

        if profile:
            topics = project_profile_topics(
                profile.frequent_topics[:5], allow_stable=allow_stable
            )
            if not allow_stable and profile.frequent_topics:
                logger.info(
                    "profile scope: stable-tier profile fields withheld "
                    "(no cross-session consent) user={}",
                    user_id,
                )
            parts.append(
                redact_pii(
                    f"用户历史：共 {profile.total_sessions} 次会话，"
                    f"关注领域：{', '.join(topics) or '未记录'}"
                )
            )
            # Phase 29（Gotcha 3）：profile_card 进 prompt = 前缀变更 ⇒
            # **仅 flag 开时拼装**；flag-off 不出现该段，拼装与基线字节等价。
            if (
                settings.MEMORY_PROFILE_CARD_ENABLED
                and profile.profile_card
            ):
                card = project_profile_card(
                    profile.profile_card, allow_stable=allow_stable
                )
                if card:
                    parts.append(
                        "用户画像卡（profile_card）："
                        + redact_pii(
                            json.dumps(
                                card, ensure_ascii=False, sort_keys=True
                            )
                        )
                    )

        if summaries:
            parts.append("相关历史对话摘要：")
            for i, mem in enumerate(summaries, 1):
                parts.append(f"  {i}. {redact_pii(mem.summary)}")

        cards_part = await self._cards_context_or_empty(user_id)
        if cards_part:
            if allow_stable:
                parts.append(cards_part)
            else:
                logger.info(
                    "profile scope: semantic cards withheld (no cross-session consent) user={}",
                    user_id,
                )

        return "\n".join(parts)

    async def _cards_context_or_empty(self, user_id: str = "default_user") -> str:
        """R3-8：语义卡片区段（MEMORY_CARDS_ENABLED，默认开但挂在
        MEMORY_CONTEXT_ENABLED 通道之下；异常/关闭返回空串不影响主链路）。"""
        if not getattr(settings, "MEMORY_CARDS_ENABLED", False):
            return ""
        try:
            from agentix.application.services.semantic_memory import SemanticMemoryStore

            store = SemanticMemoryStore(redis_client=self._redis, prefix=self.prefix)
            cards = await store.recall_cards(user_id, limit=5)
            if not cards:
                return ""
            # B1·M-P0-3（读路径二次脱敏）：legacy 原文卡片兜底——出 prompt 必脱敏
            return "用户稳定偏好（长期记忆）：" + "；".join(
                redact_pii(c.content) for c in cards
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("semantic card recall failed (non-fatal): {}", exc)
            return ""

    async def clear_user_memories(self, user_id: str) -> int:
        """Clear all long-term memories for a user."""
        user_index_key = f"{self.prefix}:user:{user_id}:sessions"
        session_ids = await self._redis.zrange(user_index_key, 0, -1)

        count = 0
        for session_id in session_ids:
            deleted = await self._redis.delete(f"{self.prefix}:summary:{session_id}")
            count += deleted

        await self._redis.delete(user_index_key)
        await self._redis.delete(f"{self.prefix}:profile:{user_id}")

        logger.info("Cleared long-term memory: user={} count={}", user_id, count)
        return count

    async def clear_user_profile_chain(self, user_id: str) -> Dict[str, Any]:
        """F2a·M-P2-4 一键清除：画像（含 ``profile_card``）+ 语义卡 + 摘要全链路。

        - **分段执行、逐段计数**：删除是隐私权利——中段失败也继续后续段，
          全部尝试完再汇总（``errors`` 非空 ⇒ 调用方应显式失败，不静默吞）；
        - 摘要 + 用户索引 + 画像经 :meth:`clear_user_memories`（顺带撤销），
          语义卡经 ``SemanticMemoryStore.clear_cards``，跨会话沿用授权
          （M-P2-4）随画像一并撤销——清除后不残留授权状态。

        :return: ``{"summaries": n, "profile_cleared": bool,
            "semantic_cards": n, "scope_consent_revoked": bool, "errors": [...]}``
        """
        errors: List[str] = []
        summaries = 0
        profile_cleared = False
        consent_revoked = False
        cards = 0
        try:
            summaries = await self.clear_user_memories(user_id)
            profile_cleared = True
        except Exception as exc:  # noqa: BLE001 - 不因中段失败放弃后续段
            errors.append(f"summaries/profile: {exc}")
        try:
            had_consent = await self.profile_scope_consent(user_id)
            if had_consent:
                await self.set_profile_scope_consent(user_id, granted=False)
            consent_revoked = True  # 授权键已确认不存在/已撤销
        except Exception as exc:  # noqa: BLE001
            errors.append(f"scope_consent: {exc}")
        try:
            from agentix.application.services.semantic_memory import SemanticMemoryStore

            store = SemanticMemoryStore(redis_client=self._redis, prefix=self.prefix)
            cards = await store.clear_cards(user_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"semantic_cards: {exc}")
        if errors:
            logger.error(
                "clear_user_profile_chain partial failure: user={} errors={}",
                user_id,
                errors,
            )
        else:
            logger.info(
                "clear_user_profile_chain done: user={} summaries={} cards={}",
                user_id,
                summaries,
                cards,
            )
        return {
            "user_id": user_id,
            "summaries": summaries,
            "profile_cleared": profile_cleared,
            "semantic_cards": cards,
            "scope_consent_revoked": consent_revoked,
            "errors": errors,
        }


_long_term_memory_instance: Optional[LongTermMemory] = None


def get_long_term_memory() -> LongTermMemory:
    """Get or create the global long-term memory instance."""
    global _long_term_memory_instance
    if _long_term_memory_instance is None:
        _long_term_memory_instance = LongTermMemory()
    return _long_term_memory_instance
