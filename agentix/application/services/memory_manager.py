"""P6-6.1 记忆扇出（MemoryManager）+ Redis 长时记忆适配器（§5 表 6.1 落点）。

借鉴 source-code-study §3.5（hermes MemoryManager 多 provider 扇出 + provider ABC）：

- ``MemoryManager`` 把 :class:`MemoryPort` 的 recall/remember 扇出到多个
  provider——每 provider 独立超时、失败记 warning 跳过（recall fail-open
  永不抛，全败返回 ``[]``；remember 全败才抛，诚实上报不静默丢写）；
- ``LongTermMemoryProvider`` 把 MemoryPort 语义适配到 Redis 长时记忆，
  **独立键空间** ``{prefix}:port:{subject}``（JSON hash，field=item_id）——
  不复用 ``{prefix}:summary:*``，避免把记忆条目算进 ``profile.total_sessions``
  或重复注入摘要区，且 kind/confidence/tags 全保真（摘要存储做不到）；
- 生产装配走 ``get_memory_manager()`` 懒加载单例：仅被
  ``MEMORY_FANOUT_ENABLED`` 的消费点调用（flag-off 零接线零开销）。

分层：application → agent/domain（application→agent 既有 5 处先例）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from threading import Lock
from typing import Any, Dict, List, Optional, Sequence, Tuple

from loguru import logger
from redis.asyncio import Redis

from agentix.agent.memory.service import InMemoryMemory, redact_pii, resolve_conflicts
from agentix.config.settings import settings
from agentix.domain.agent.types import MemoryItem, MemoryKind, MemoryScope
from agentix.domain.agent.ports import MemoryPort

__all__ = [
    "PROVIDER_TIMEOUT_S",
    "MemoryManager",
    "MemoryScopeUnsupported",
    "LongTermMemoryProvider",
    "get_memory_manager",
    "reset_memory_manager",
]

#: 单 provider 召回/写入超时（秒）——慢 provider 不拖垮整体扇出。
PROVIDER_TIMEOUT_S = 1.5


class MemoryScopeUnsupported(ValueError):
    """Provider 不支持该 scope（扇出时跳过该 provider，不算失败）。"""


def _dedup_by_content(items: List[MemoryItem]) -> List[MemoryItem]:
    """同 (scope, subject, content) 去重，保 confidence 最高，首位出现序稳定。"""
    best: Dict[Tuple[str, str, str], MemoryItem] = {}
    order: List[Tuple[str, str, str]] = []
    for it in items:
        key = (it.scope.value, it.subject, it.content.strip())
        if key not in best:
            best[key] = it
            order.append(key)
        elif it.confidence > best[key].confidence:
            best[key] = it
    return [best[k] for k in order]


class MemoryManager:
    """多 provider 记忆扇出（§3.5 hermes MemoryManager 借鉴）。

    契约：

    - ``recall``：每 provider 独立超时；失败 provider 记 warning 跳过
      （fail-open，**永不抛**——全败返回 ``[]``）；合并经内容去重 +
      ``resolve_conflicts`` 裁决 + confidence 降序（稳定排序，平分按
      provider 序/首见序）后截断 ``limit``；
    - ``remember``：扇出到全部 provider；scope 不支持算**跳过**（不计失败），
      其他异常记 warning 并继续；≥1 成功返回首个 item_id，全败抛
      ``RuntimeError``（honest——不把失败伪装成成功）。
    """

    def __init__(
        self,
        providers: Sequence[Any],
        *,
        timeout_s: float = PROVIDER_TIMEOUT_S,
    ) -> None:
        if not providers:
            raise ValueError("MemoryManager requires at least one provider")
        for provider in providers:
            # runtime_checkable Protocol：只查方法面（ctor 边界 fail-fast，
            # 借鉴 hermes provider ABC 的 isinstance 校验）。
            if not isinstance(provider, MemoryPort):
                raise TypeError(
                    f"provider {type(provider).__name__} does not implement MemoryPort"
                )
        self._providers = tuple(providers)
        self._timeout_s = float(timeout_s)
        self._last_degraded: Tuple[str, ...] = ()

    @property
    def last_degraded(self) -> Tuple[str, ...]:
        """上次 recall 中降级（超时/异常被跳过）的 provider 类名。"""
        return self._last_degraded

    async def recall(
        self,
        *,
        scope: str,
        subject: str,
        query: str = "",
        limit: int = 10,
    ) -> List[MemoryItem]:
        async def _one(provider: Any) -> List[MemoryItem]:
            return await asyncio.wait_for(
                provider.recall(scope=scope, subject=subject, query=query, limit=limit),
                self._timeout_s,
            )

        results = await asyncio.gather(
            *(_one(p) for p in self._providers), return_exceptions=True
        )
        merged: List[MemoryItem] = []
        degraded: List[str] = []
        for provider, result in zip(self._providers, results):
            if isinstance(result, BaseException):
                degraded.append(type(provider).__name__)
                logger.warning(
                    "memory provider recall degraded ({}): {}",
                    type(provider).__name__,
                    result,
                )
                continue
            merged.extend(result)
        self._last_degraded = tuple(degraded)
        resolved = resolve_conflicts(_dedup_by_content(merged))
        resolved.sort(key=lambda it: it.confidence, reverse=True)
        return resolved[: max(0, limit)]

    async def remember(self, item: MemoryItem) -> str:
        successes: List[str] = []
        failures: List[str] = []
        for provider in self._providers:
            try:
                item_id = await asyncio.wait_for(
                    provider.remember(item), self._timeout_s
                )
            except MemoryScopeUnsupported as exc:
                logger.debug(
                    "memory provider skipped ({}): {}", type(provider).__name__, exc
                )
                continue
            except Exception as exc:  # noqa: BLE001 - 单 provider 失败不中断扇出
                failures.append(f"{type(provider).__name__}: {exc}")
                logger.warning(
                    "memory provider remember failed ({}): {}",
                    type(provider).__name__,
                    exc,
                )
            else:
                successes.append(item_id)
        if successes:
            return successes[0]
        if failures:
            raise RuntimeError(
                "no memory provider accepted item: " + "; ".join(failures)
            )
        raise RuntimeError(f"no memory provider supports scope={item.scope.value!r}")

    async def forget(
        self,
        *,
        scope: str,
        subject: str,
        item_id: str = "",
        content: str = "",
    ) -> int:
        """删除匹配条目（A2c 记忆三工具），扇出到**声明了 forget 的 provider**。

        - duck-typed 可选方法：``MemoryPort`` Protocol 不扩容（runtime_checkable
          isinstance 契约不动），没实现 ``forget`` 的 provider 静默跳过；
        - scope 不支持算跳过、单 provider 失败记 warning 继续（与 recall 同款
          fail-open）；返回成功删除总数（0 = 无 provider 匹配到）。
        """
        deleted = 0
        for provider in self._providers:
            fn = getattr(provider, "forget", None)
            if not callable(fn):
                continue
            try:
                count = await asyncio.wait_for(
                    fn(scope=scope, subject=subject, item_id=item_id, content=content),
                    self._timeout_s,
                )
            except MemoryScopeUnsupported as exc:
                logger.debug(
                    "memory provider forget skipped ({}): {}",
                    type(provider).__name__,
                    exc,
                )
                continue
            except Exception as exc:  # noqa: BLE001 - 单 provider 失败不中断扇出
                logger.warning(
                    "memory provider forget failed ({}): {}",
                    type(provider).__name__,
                    exc,
                )
                continue
            deleted += int(count or 0)
        return deleted


class LongTermMemoryProvider:
    """MemoryPort ↔ Redis 长时记忆适配器（P6-6.1，§5 表 6.1）。

    - ``recall``：**scope 隔离**——只承载 USER 画像条目，其他 scope 返回
      ``[]``；坏 JSON 条目告警跳过（granular fail-open）；排序
      ``(-confidence, item_id)`` 完全确定（不依赖 hash 迭代序）；
    - ``remember``：scope≠USER 抛 :class:`MemoryScopeUnsupported`（扇出层
      记跳过）；内容过 ``redact_pii``；无 item_id 时按脱敏内容 sha256 前缀
      生成**确定性 id**（同内容幂等覆盖）；超出 ``max_items`` 按
      ``stored_at`` 裁剪最旧（删-写语义与 ``SemanticMemoryStore`` 一致）；
    - Redis 异常**不吞**（本层诚实抛出，fail-open 由 MemoryManager 扇出层负责）。
    """

    name = "redis_ltm"

    def __init__(
        self,
        *,
        redis_client: Any = None,
        prefix: str = "ltm",
        max_items: int = 200,
    ) -> None:
        self._redis = redis_client
        self._prefix = prefix
        self._max_items = max(1, int(max_items))

    def _client(self) -> Any:
        if self._redis is None:
            self._redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        return self._redis

    def _key(self, subject: str) -> str:
        return f"{self._prefix}:port:{subject}"

    async def recall(
        self,
        *,
        scope: str,
        subject: str,
        query: str = "",
        limit: int = 10,
    ) -> List[MemoryItem]:
        if scope != MemoryScope.USER.value:
            return []  # scope 隔离：适配器只承载 USER 画像条目
        raw = await self._client().hgetall(self._key(subject))
        items: List[MemoryItem] = []
        for field, payload in (raw or {}).items():
            try:
                data = json.loads(payload)
                items.append(
                    MemoryItem(
                        content=str(data["content"]),
                        kind=MemoryKind(data.get("kind", MemoryKind.EPISODIC.value)),
                        scope=MemoryScope(
                            data.get("scope", MemoryScope.USER.value)
                        ),
                        item_id=str(field),
                        subject=str(data.get("subject", subject)),
                        confidence=float(data.get("confidence", 1.0)),
                        tags=tuple(data.get("tags", ())),
                        meta=dict(data.get("meta", {})),
                    )
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                logger.warning(
                    "port item decode skipped (subject={} id={})", subject, field
                )
        if query:
            q = query.lower()
            items = [
                it
                for it in items
                if q in it.content.lower() or any(q in t.lower() for t in it.tags)
            ]
        items.sort(key=lambda it: (-it.confidence, it.item_id))
        return items[: max(0, limit)]

    async def remember(self, item: MemoryItem) -> str:
        if item.scope != MemoryScope.USER:
            raise MemoryScopeUnsupported(
                f"redis_ltm only stores scope={MemoryScope.USER.value!r}, "
                f"got {item.scope.value!r}"
            )
        subject = (item.subject or "").strip()
        if not subject:
            raise ValueError("redis_ltm remember requires a non-empty subject (user_id)")
        redacted = redact_pii(item.content)
        item_id = item.item_id or hashlib.sha256(redacted.encode("utf-8")).hexdigest()[:16]
        payload = json.dumps(
            {
                "content": redacted,
                "kind": item.kind.value,
                "scope": item.scope.value,
                "subject": subject,
                "confidence": item.confidence,
                "tags": list(item.tags),
                "meta": {
                    **item.meta,
                    "stored_at": time.time(),
                    "redacted": redacted != item.content,
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        key = self._key(subject)
        client = self._client()
        await client.hset(key, mapping={item_id: payload})
        raw = await client.hgetall(key)
        if len(raw) > self._max_items:
            stamped: List[Tuple[float, str]] = []
            for field, stored in raw.items():
                try:
                    at = float(json.loads(stored).get("meta", {}).get("stored_at", 0.0))
                except (json.JSONDecodeError, TypeError, ValueError):
                    at = 0.0
                stamped.append((at, str(field)))
            stamped.sort(reverse=True)  # 新者在前；平分按 id 降序（确定）
            keep_ids = {fid for _, fid in stamped[: self._max_items]}
            await client.delete(key)
            await client.hset(
                key, mapping={fid: raw[fid] for fid in keep_ids if fid in raw}
            )
        return item_id

    async def forget(
        self,
        *,
        scope: str,
        subject: str,
        item_id: str = "",
        content: str = "",
    ) -> int:
        """删除匹配条目（A2c）：hdel 精确 field 或内容匹配的 field。

        scope≠USER 抛 :class:`MemoryScopeUnsupported`（适配器只承载 USER，
        扇出层记跳过）；内容匹配同时比对原文与脱敏后文本（存储侧已脱敏）。
        """
        if scope != MemoryScope.USER.value:
            raise MemoryScopeUnsupported(
                f"redis_ltm forget only supports scope={MemoryScope.USER.value!r}, "
                f"got {scope!r}"
            )
        subject = (subject or "").strip()
        if not subject:
            raise ValueError("redis_ltm forget requires a non-empty subject (user_id)")
        key = self._key(subject)
        client = self._client()
        raw = await client.hgetall(key)
        if not raw:
            return 0
        redacted = redact_pii(content) if content else ""
        doomed: List[str] = []
        for field, payload in raw.items():
            if item_id and field == item_id:
                doomed.append(field)
                continue
            if content:
                try:
                    stored_content = str(json.loads(payload).get("content", ""))
                except (json.JSONDecodeError, TypeError):
                    continue
                if stored_content in (content, redacted):
                    doomed.append(field)
        if doomed:
            await client.hdel(key, *doomed)
        return len(doomed)


_manager: Optional[MemoryManager] = None
_manager_lock = Lock()


def get_memory_manager() -> MemoryManager:
    """懒加载单例（Redis 适配器延迟到首次 IO 才建连接客户端）。"""
    global _manager
    manager = _manager
    if manager is None:
        with _manager_lock:
            manager = _manager
            if manager is None:
                manager = MemoryManager([LongTermMemoryProvider(), InMemoryMemory()])
                _manager = manager
    return manager


def reset_memory_manager() -> None:
    """清空单例（测试与显式重建用）。"""
    global _manager
    with _manager_lock:
        _manager = None
