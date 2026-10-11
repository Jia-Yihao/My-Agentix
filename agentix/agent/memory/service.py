"""Memory 服务（Phase 4 → B1·M-P0-5 收口）：三 scope × 五 kind，读取时冲突裁决（时间窗 + 标记），PII 脱敏写读双向。

实现满足 MemoryPort；**适配不重写**——后续可包一层去调 long_term/semantic_memory。
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from agentix.config import settings
from agentix.domain.agent.types import MemoryItem

__all__ = [
    "CONFLICT_TIME_WINDOW_DAYS",
    "InMemoryMemory",
    "redact_pii",
    "resolve_conflicts",
]

#: F1b·M-P1-1：单次 embedding 内部预算（秒）。MemoryManager 的 provider 超时
#: 是 1.5s——embedding 必须在 provider 超时前失败并回关键词路径，否则
#: 「向量不可用时关键词 fail-open」会退化成整 provider 降级（关键词一并丢）。
_EMBED_BUDGET_S = 1.0

#: B1·M-P0-4：同键冲突裁决时间窗（天）。窗内同键 = 同一事实的新旧版本 →
#: 裁决并**标记**；窗外 = 历史值 → 独立成簇各自保留（新旧不互相裁决/覆盖）。
CONFLICT_TIME_WINDOW_DAYS = 30.0

# 粗粒度 PII 脱敏（写入路径；日志勿再打原文）。
# 基线四条（PHONE/EMAIL/ID/CARD）**只增不减**；其后为 B1·M-P0-3 增补规则。
_PII_PATTERNS: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b1[3-9]\d{9}\b"), "[PHONE]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[EMAIL]"),
    (re.compile(r"\b\d{17}[\dXx]\b"), "[ID]"),
    (
        re.compile(
            r"\b(?:\d{4}[- ]?){3}\d{4}\b|"
            r"\b(?:62|4|5)\d{14,17}\b"
        ),
        "[CARD]",
    ),
    # -- B1·M-P0-3（只增）：中文地址（行政区划起手 / 道路门牌 / 楼栋室 / 小区房号）--
    (
        re.compile(
            r"[\u4e00-\u9fa5]{1,12}(?:省|市|自治区)[\u4e00-\u9fa5]{0,12}"
            r"(?:区|县|市)?[\u4e00-\u9fa50-9]{0,20}"
            r"(?:路|街|大道|道|巷|弄)\d+(?:号|附\d+号)"
        ),
        "[ADDR]",
    ),
    (
        re.compile(r"[\u4e00-\u9fa50-9]{1,20}(?:路|街|大道|巷|弄)\d+(?:号|附\d+号)"),
        "[ADDR]",
    ),
    (re.compile(r"\d+号楼\d+(?:单元)?\d*(?:室|号)"), "[ADDR]"),
    (re.compile(r"\d+栋\d+(?:单元)?\d*(?:室|号)"), "[ADDR]"),
    (
        re.compile(
            r"[\u4e00-\u9fa5]{2,10}(?:小区|花园|公寓|大厦|新村)"
            r"\d*栋?\d*单元?\d*(?:室|号)"
        ),
        "[ADDR]",
    ),
    # -- B1·M-P0-3（只增）：英文地址（门牌 + 道路通名；Title Case 故意不用 re.I）--
    (
        re.compile(
            r"\b\d{1,5}\s+[A-Za-z][A-Za-z'.-]*(?:\s+[A-Za-z][A-Za-z'.-]*){0,7}"
            r"\s+(?:Street|Road|Avenue|Lane|Boulevard|Drive|Way|Court|Place)\b"
        ),
        "[ADDR]",
    ),
    # -- B1·M-P0-3（只增）：中文姓名（自我介绍/冒号标注锚点——防法律文本误伤）--
    (re.compile(r"(?:我叫|我名叫)[\u4e00-\u9fa5]{2,4}"), "[NAME]"),
    (
        re.compile(r"(?:姓名|联系人|收件人|客户姓名)\s*[:：]\s*[\u4e00-\u9fa5]{2,4}"),
        "[NAME]",
    ),
    # -- B1·M-P0-3（只增）：英文姓名（声明式锚点）--
    (re.compile(r"(?i)my name is [A-Z][a-z]+(?:\s+[A-Z][a-z]+)?"), "[NAME]"),
    (re.compile(r"\bI'm [A-Z][a-z]+(?:\s+[A-Z][a-z]+)?"), "[NAME]"),
    (re.compile(r"\bI am [A-Z][a-z]+(?:\s+[A-Z][a-z]+)?"), "[NAME]"),
)


def redact_pii(text: str) -> str:
    out = text
    for pat, repl in _PII_PATTERNS:
        out = pat.sub(repl, out)
    return out


def _conflict_key(it: MemoryItem) -> Tuple[str, str, Tuple[str, ...]]:
    """B1·M-P0-4：键 = ``(kind, subject, **全 tags**)``——不再只取 ``tags[0]``。"""
    return (it.kind.value, it.subject, tuple(sorted({str(t) for t in it.tags})))


def _stored_at(it: MemoryItem) -> Optional[float]:
    value = it.meta.get("stored_at")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _loser_row(it: MemoryItem) -> Dict[str, Any]:
    return {
        "item_id": it.item_id,
        "content": redact_pii(it.content),
        "confidence": it.confidence,
        "stored_at": _stored_at(it),
    }


def _mark_conflicts(winner: MemoryItem, losers: List[MemoryItem]) -> MemoryItem:
    """把被裁决值**标记**进胜者 ``meta["conflicts"]``（只标记不静默丢/不覆写）。"""
    if not losers:
        return winner
    prior = [dict(r) for r in (winner.meta.get("conflicts") or []) if isinstance(r, dict)]
    seen = {(r.get("item_id"), r.get("content")) for r in prior}
    for loser in losers:
        row = _loser_row(loser)
        marker = (row["item_id"], row["content"])
        if marker not in seen:
            prior.append(row)
            seen.add(marker)
    return replace(winner, meta={**winner.meta, "conflicts": prior})


def _resolve_group(
    pairs: List[Tuple[int, MemoryItem]], *, window_seconds: float
) -> List[MemoryItem]:
    # 时间序（未知时间戳按「当下」排最前、其次最新在前、平分保首见），随后按窗分簇
    ordered = sorted(
        pairs,
        key=lambda p: (
            0 if _stored_at(p[1]) is None else 1,
            -(_stored_at(p[1]) or 0.0),
            p[0],
        ),
    )
    clusters: List[List[Tuple[int, MemoryItem]]] = []
    anchor: Optional[float] = None  # 簇内最新已知时间（None = 尚无已知时间戳）
    for pair in ordered:
        ts = _stored_at(pair[1])
        if not clusters:
            clusters.append([pair])
            anchor = ts
            continue
        within = ts is None or anchor is None or (anchor - ts) <= window_seconds
        if within:
            clusters[-1].append(pair)
            if ts is not None:
                anchor = ts if anchor is None else max(anchor, ts)
        else:
            clusters.append([pair])
            anchor = ts

    winners: List[MemoryItem] = []
    for cluster in clusters:
        winner_idx, winner = max(
            cluster,
            key=lambda p: (
                p[1].confidence,
                _stored_at(p[1]) if _stored_at(p[1]) is not None else float("-inf"),
                -p[0],
            ),
        )
        losers = [it for idx, it in cluster if idx != winner_idx]
        winners.append(_mark_conflicts(winner, losers))
    return winners


def resolve_conflicts(items: List[MemoryItem]) -> List[MemoryItem]:
    """读取时冲突裁决（B1·M-P0-4 加固：全 tags 键 + 时间窗 + 标记不静默）。

    - **键**：``(kind, subject, 全 tags 排序集)``——``tags[0]`` 相同但全集
      不同 ⇒ 不同事实，不裁决；
    - **时间窗**：同键按 ``stored_at`` 30 天窗（``CONFLICT_TIME_WINDOW_DAYS``）
      分簇，窗外历史独立保留（各出胜者，新旧不互相覆盖）；
    - **窗内同簇**：保留 confidence 最高为胜者（同分取新、再同保首见），
      被裁决值**标记**进胜者 ``meta["conflicts"]``（行含 item_id/content/
      confidence/stored_at——只标记不静默丢，可追溯，不覆写不删除）；
    - ``stored_at`` 未知 ⇒ 视为当下（参与裁决，保持无时间戳存量数据的旧语义）。

    书 §3：默认 read-time resolution；写入只做去重提示，不在此合并。
    纯函数：不修改输入（MemoryItem frozen，标记经 ``replace`` 返回新条目）。
    """
    groups: Dict[Tuple[str, str, Tuple[str, ...]], List[Tuple[int, MemoryItem]]] = {}
    order: List[Tuple[str, str, Tuple[str, ...]]] = []
    for index, it in enumerate(items):
        key = _conflict_key(it)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((index, it))

    window_seconds = CONFLICT_TIME_WINDOW_DAYS * 86400.0
    out: List[MemoryItem] = []
    for key in order:
        out.extend(_resolve_group(groups[key], window_seconds=window_seconds))
    return out


class InMemoryMemory:
    """进程内记忆（scope 隔离 + PII 脱敏 + 读取时裁决）。"""

    def __init__(self) -> None:
        self._items: List[MemoryItem] = []
        # F1b·M-P1-1：item_id → embedding（进程内 provider 与条目同生命周期；
        # flag-off 时恒为空表，零额外开销）。
        self._vectors: Dict[str, List[float]] = {}

    async def recall(
        self,
        *,
        scope: str,
        subject: str,
        query: str = "",
        limit: int = 10,
    ) -> List[MemoryItem]:
        hits = [
            it
            for it in self._items
            if it.scope.value == scope and it.subject == subject
        ]
        # F1b·M-P1-1（memory-optimization §5）：flag-on 且有查询 ⇒ 关键词 ×
        # 向量双路 + recency 衰减；向量侧任何失败（embedding 服务不可达 /
        # 超预算 / 空向量）⇒ 打点后落回下面的纯关键词路径（fail-open）。
        if settings.MEMORY_VECTOR_RECALL_ENABLED and query:
            try:
                return await self._recall_vector(hits, query=query, limit=limit)
            except Exception as exc:  # noqa: BLE001 - 向量不可用必须能答
                logger.warning(
                    "vector recall degraded, keyword fallback (non-fatal): {}", exc
                )
        if query:
            q = query.lower()
            hits = [it for it in hits if q in it.content.lower() or any(q in t for t in it.tags)]
        hits = resolve_conflicts(hits)
        # B1·M-P0-3（读路径二次脱敏）：legacy 直存原文兜底——出门必脱敏（幂等）。
        hits = [_redact_item(it) for it in hits]
        hits.sort(key=lambda x: x.confidence, reverse=True)
        return hits[: max(0, limit)]

    async def _recall_vector(
        self, pool: List[MemoryItem], *, query: str, limit: int
    ) -> List[MemoryItem]:
        """M-P1-1 双路召回：候选 = 全 pool（冲突裁决后），融合打分排序。

        与关键词路径的差异是刻意的：向量能召回**不含子串**的语义近邻，故候选
        集不能先被子串过滤掉；读路径二次脱敏（M-P0-3）与 limit 截断保持一致。
        任何异常向上抛，由 :meth:`recall` 捕获后 fail-open 回关键词路径。
        """
        from agentix.domain.policies.memory_recall import (
            cosine_similarity,
            fuse_recall_scores,
            weights_for,
        )

        resolved = resolve_conflicts(pool)
        q_lower = query.lower()
        keyword_hits = {
            it.item_id
            for it in resolved
            if it.item_id
            and (
                q_lower in it.content.lower()
                or any(q_lower in t for t in it.tags)
            )
        }
        embedded = await self._embed_texts([query])
        query_vec = embedded[0] if embedded else []
        if not query_vec:
            raise RuntimeError("empty query embedding")
        vector_scores: Dict[str, float] = {}
        for it in resolved:
            if not it.item_id:
                continue
            item_vec = self._vectors.get(it.item_id)
            if item_vec:
                vector_scores[it.item_id] = cosine_similarity(query_vec, item_vec)
        weights = weights_for(
            resolved, half_life_days=settings.MEMORY_RECENCY_HALF_LIFE_DAYS
        )
        ordered = fuse_recall_scores(
            [it.item_id or "" for it in resolved],
            keyword_hits,
            vector_scores,
            weights,
        )
        by_id = {it.item_id: it for it in resolved if it.item_id}
        out: List[MemoryItem] = []
        for iid, score in ordered:
            # 双通道皆未命中（融合分 0）= 与查询无关 ⇒ 不返回，保持与关键词
            # 路径一致的「只给命中」语义（recency 只重排命中项，不造命中）。
            if score <= 0.0:
                continue
            item = by_id.get(iid)
            if item is None:
                continue
            out.append(_redact_item(item))
            if len(out) >= max(0, limit):
                break
        return out

    async def _embed_texts(self, texts: List[str]) -> List[List[float]]:
        """复用检索域向量栈做 embedding（懒 import + 内部预算，超时即失败）。"""

        def _call() -> List[List[float]]:
            from agentix.retrieval.infra.rag.dense import embed_texts_sync

            return embed_texts_sync(list(texts))

        return await asyncio.wait_for(asyncio.to_thread(_call), _EMBED_BUDGET_S)

    async def _try_embed(self, item: MemoryItem) -> None:
        """写路径 best-effort 挂向量（失败只打点，绝不影响 remember）。"""
        try:
            vecs = await self._embed_texts([item.content])
            if vecs and vecs[0] and item.item_id:
                self._vectors[item.item_id] = vecs[0]
        except Exception as exc:  # noqa: BLE001 - 写路径 fail-open
            logger.debug("memory vector embed skipped (non-fatal): {}", exc)

    async def remember(self, item: MemoryItem) -> str:
        redacted = redact_pii(item.content)
        item_id = item.item_id or uuid.uuid4().hex
        stored = MemoryItem(
            content=redacted,
            kind=item.kind,
            scope=item.scope,
            item_id=item_id,
            subject=item.subject,
            confidence=item.confidence,
            tags=item.tags,
            meta={**item.meta, "stored_at": time.time(), "redacted": redacted != item.content},
        )
        # 去重：同 scope+subject+content 覆盖 confidence
        for i, existing in enumerate(self._items):
            if (
                existing.scope == stored.scope
                and existing.subject == stored.subject
                and existing.content == stored.content
            ):
                if stored.confidence >= existing.confidence:
                    self._items[i] = stored
                    # F1b·M-P1-1：覆盖后同步刷新向量（flag-off 恒零开销）
                    if settings.MEMORY_VECTOR_RECALL_ENABLED:
                        await self._try_embed(stored)
                return self._items[i].item_id
        self._items.append(stored)
        if settings.MEMORY_VECTOR_RECALL_ENABLED:
            await self._try_embed(stored)
        return item_id

    def all_items(self) -> List[MemoryItem]:
        return list(self._items)

    async def forget(
        self,
        *,
        scope: str,
        subject: str,
        item_id: str = "",
        content: str = "",
    ) -> int:
        """删除匹配条目（A2c）：scope+subject 隔离，按 id 或内容精确匹配。

        item_id 与 content 至少给一个（都不给 → 0，绝不误清全库）；返回删除数。
        """
        if not item_id and not content:
            return 0
        before = len(self._items)
        redacted = redact_pii(content) if content else ""
        self._items = [
            it
            for it in self._items
            if not (
                it.scope.value == scope
                and it.subject == subject
                and (
                    (item_id and it.item_id == item_id)
                    or (content and it.content in (content, redacted))
                )
            )
        ]
        # F1b·M-P1-1：随删清理向量（删彻底纪律；flag-off 时表恒空，零开销）。
        if self._vectors:
            kept = {it.item_id for it in self._items}
            self._vectors = {
                k: v for k, v in self._vectors.items() if k in kept
            }
        return before - len(self._items)


def _redact_item(it: MemoryItem) -> MemoryItem:
    """B1·M-P0-3：条目正文与冲突标记行一并脱敏（幂等；不改原对象）。"""
    content = redact_pii(it.content)
    meta = it.meta
    rows = meta.get("conflicts")
    if isinstance(rows, list) and rows:
        meta = {
            **meta,
            "conflicts": [
                {
                    **row,
                    "content": redact_pii(str(row.get("content") or "")),
                }
                if isinstance(row, dict)
                else row
                for row in rows
            ],
        }
    if content == it.content and meta is it.meta:
        return it
    return replace(it, content=content, meta=meta)
