"""RedisStreamEventBus：EventPort 的 Redis Stream 实现（Phase 23，Ch6 §6.2）。

布局（``agentix:events:`` 前缀，全部键名可经 ``prefix`` 覆盖）：

- ``stream:{event_type}`` — XADD 待投递队列；投递成功/过期后 XDEL
  （⇒ 重启后 delivered 不重投，与 Jsonl 的 state 快照语义对齐）；
- ``rec:{event_id}`` — 事件快照（STRING json，``EventRecord.to_dict()`` 同形），
  供 ``get``/``list_recent``/``deliver_with_retry`` 读取，投递后仍保留；
- ``dedup:{event_type}:{event_key}`` — SET NX 去重键（跨进程/跨重启唯一，
  同 ``event_key`` 二次 publish 返回首次 ``event_id``）；
- ``index`` — 最近事件 id 列表（RPUSH + LTRIM ≤2000，``list_recent`` 用）。

**分层决策**（规划 §3 原文倾向 infrastructure，此处落 agent 层，理由记档）：
memory/jsonl 两个 EventPort 实现均居 ``agentix/agent/events/``；全仓无
agent→infrastructure import 先例；domain Protocol 不动、redis 懒 import
（包缺失时模块仍可导入，p40 常绿）；查询面 ``list_recent`` 是鸭子扩展
（非 ``EventPort`` 协议成员）。

无 Redis 诚实失败/诚实 skip：连接错误在命令面**向上传播**（不吞不伪造），
契约测试以 ping 探活决定 skip。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional

from loguru import logger

from agentix.agent.events.adapter import (
    EventDedupKey,
    EventRecord,
    EventStatus,
    VirtualIdentity,
)

__all__ = ["RedisStreamEventBus", "DEFAULT_STREAM_PREFIX"]

#: 键前缀（settings 不新增键——与 jsonl 同款「零额外新键」决策）。
DEFAULT_STREAM_PREFIX = "agentix:events:"
#: index 列表保留上限（ops 查询窗）。
_INDEX_MAX = 2000


def _record_to_json(rec: EventRecord) -> str:
    return json.dumps(rec.to_dict(), ensure_ascii=False, default=str)


def _record_from_json(data: str) -> Optional[EventRecord]:
    try:
        d = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(d, dict) or "event_id" not in d:
        return None
    dk = d.get("dedup_key") or {}
    try:
        return EventRecord(
            event_id=str(d["event_id"]),
            event_type=str(d.get("event_type", "")),
            payload=dict(d.get("payload") or {}),
            dedup_key=EventDedupKey(
                event_type=str(dk.get("event_type", "")),
                event_key=str(dk.get("event_key", "")),
            ),
            occurred_at=float(d.get("occurred_at", 0.0)),
            received_at=float(d.get("received_at", 0.0)),
            status=EventStatus(d.get("status", EventStatus.PENDING.value)),
            attempts=int(d.get("attempts", 0)),
            max_retries=int(d.get("max_retries", 3)),
            last_error=str(d.get("last_error", "")),
            expires_at=d.get("expires_at"),
        )
    except (ValueError, TypeError):
        return None


class RedisStreamEventBus:
    """``EventPort`` 的 Redis Stream 实现（语义见模块 docstring）。

    构造**不连库**（客户端懒建）；首个命令才触达网络，连接错误向上传播。
    """

    def __init__(
        self,
        url: Optional[str] = None,
        *,
        prefix: str = DEFAULT_STREAM_PREFIX,
        identity: Optional[VirtualIdentity] = None,
        client: Any = None,
    ) -> None:
        self._url = url
        self._prefix = prefix
        self._identity = identity or VirtualIdentity.mint(
            scopes=("event:publish", "event:wait")
        )
        self._client = client  # 测试可注入；None ⇒ 首用时懒建
        # Phase H · W4 批写缓冲：(rec, dedup_key|None)；flag 关恒为空。
        self._pending: list[tuple[EventRecord, Optional[str]]] = []
        self._flush_handle: Optional[asyncio.TimerHandle] = None
        self._flush_task: Optional[asyncio.Task] = None

    # -- 客户端 -----------------------------------------------------------

    def _r(self) -> Any:
        if self._client is None:
            from agentix.config.settings import settings

            import redis.asyncio as redis_async  # 懒 import（可选依赖，p40 安全）

            url = self._url or str(settings.REDIS_URL)
            self._client = redis_async.Redis.from_url(
                url, decode_responses=True, socket_connect_timeout=2
            )
        return self._client

    def _stream_key(self, event_type: str) -> str:
        return f"{self._prefix}stream:{event_type}"

    def _rec_key(self, event_id: str) -> str:
        return f"{self._prefix}rec:{event_id}"

    def _dedup_key(self, key: EventDedupKey) -> str:
        return f"{self._prefix}dedup:{key.event_type}:{key.event_key}"

    @property
    def index_key(self) -> str:
        return f"{self._prefix}index"

    # -- 记录读写 ---------------------------------------------------------

    async def _load(self, event_id: str) -> Optional[EventRecord]:
        raw = await self._r().get(self._rec_key(event_id))
        if not raw:
            return None
        return _record_from_json(str(raw))

    async def _save(self, rec: EventRecord) -> None:
        await self._r().set(self._rec_key(rec.event_id), _record_to_json(rec))

    # -- EventPort --------------------------------------------------------

    async def publish(
        self,
        event_type: str,
        payload: Dict[str, Any],
        *,
        event_key: Optional[str] = None,
        occurred_at: Optional[float] = None,
        ttl_s: float = 0.0,
        max_retries: int = 3,
    ) -> str:
        if not self._identity.can("event:publish"):
            raise PermissionError("virtual identity lacks event:publish scope")

        key = EventDedupKey.make(event_type, event_key)
        r = self._r()
        now = time.time()
        rec = EventRecord(
            event_id=uuid.uuid4().hex,
            event_type=event_type,
            payload=dict(payload),
            dedup_key=key,
            occurred_at=occurred_at if occurred_at is not None else now,
            received_at=now,
            max_retries=max_retries,
            expires_at=(now + ttl_s) if ttl_s > 0 else None,
        )
        if key.event_key:
            # SET NX 直接占真实 id（无空串占位窗口）：输者读到的必是赢家 id
            won = await r.set(self._dedup_key(key), rec.event_id, nx=True)
            if not won:
                existing = await r.get(self._dedup_key(key))
                if existing:
                    return str(existing)  # 去重：返回首次 event_id
                # 极端竞态（键被外部清掉）：以本记录续占，避免返回空
                await r.set(self._dedup_key(key), rec.event_id, nx=True)
        try:
            if self._batching_enabled():
                # Phase H · W4 批写：dedup 仍即时（保跨进程去重语义），写四件
                # 入缓冲——满批即 flush（异常冒泡，同基线 publish 失败语义），
                # 未满定时 flush（后台吞异常+回滚 dedup，见 flush()）。
                # 权衡：可见性延迟 ≤ EVENT_BATCH_FLUSH_MS；批次写失败回滚
                # 全批 dedup（per-publish 原子放宽为批次原子，规划 §4 记档）。
                self._pending.append(
                    (rec, self._dedup_key(key) if key.event_key else None)
                )
                if len(self._pending) >= self._batch_max_size():
                    await self.flush()
                else:
                    self._schedule_flush()
            else:
                await r.xadd(self._stream_key(event_type), {"data": _record_to_json(rec)})
                await self._save(rec)
                await r.rpush(self.index_key, rec.event_id)
                await r.ltrim(self.index_key, 0, _INDEX_MAX - 1)
        except Exception:
            if key.event_key:
                try:
                    await r.delete(self._dedup_key(key))
                except Exception:  # noqa: BLE001 — 回滚尽力，原异常优先
                    pass
            raise
        return rec.event_id

    # -- Phase H · W4 批写（pipeline 批量提交；flag 关 = 零路径变化） -------

    def _batching_enabled(self) -> bool:
        """两键任一为 0 = 关（回逐条写 = 基线）。"""
        from agentix.config.settings import settings

        return (
            int(getattr(settings, "EVENT_BATCH_MAX_SIZE", 0)) > 0
            and int(getattr(settings, "EVENT_BATCH_FLUSH_MS", 0)) > 0
        )

    def _batch_max_size(self) -> int:
        from agentix.config.settings import settings

        return max(1, int(getattr(settings, "EVENT_BATCH_MAX_SIZE", 50)))

    def _schedule_flush(self) -> None:
        """首帧入缓冲后安排一次定时 flush（后续帧复用同一 handle）。"""
        if self._flush_handle is not None:
            return
        from agentix.config.settings import settings

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # publish 恒在 loop 内；防御性降级
            return
        delay_ms = max(1, int(getattr(settings, "EVENT_BATCH_FLUSH_MS", 20)))
        self._flush_handle = loop.call_later(
            delay_ms / 1000.0, self._on_flush_timer
        )

    def _on_flush_timer(self) -> None:
        self._flush_handle = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        # 保存 task 引用防被 GC；后台 flush 失败只告警（dedup 已回滚）
        self._flush_task = loop.create_task(self._flush_background())

    async def _flush_background(self) -> None:
        try:
            await self.flush()
        except Exception as exc:  # noqa: BLE001 — 定时 flush 无法向上传播
            logger.warning(
                "redis event batch flush failed ({} events rolled back): {}",
                len(self._pending),
                exc,
            )

    async def flush(self) -> int:
        """把缓冲批次经单次 pipeline 提交（xadd×N + save×N + rpush×N + ltrim）。

        失败 ⇒ 回滚本批全部 dedup 键后 re-raise（满批 publish 会看到异常，
        同基线逐条写失败语义）；返回本批条数。空缓冲是 no-op。
        """
        if not self._pending:
            return 0
        batch, self._pending = self._pending, []
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        r = self._r()
        try:
            pipe = r.pipeline(transaction=True)
            for rec, _dk in batch:
                pipe.xadd(
                    self._stream_key(rec.event_type),
                    {"data": _record_to_json(rec)},
                )
            for rec, _dk in batch:
                pipe.set(self._rec_key(rec.event_id), _record_to_json(rec))
            for rec, _dk in batch:
                pipe.rpush(self.index_key, rec.event_id)
            pipe.ltrim(self.index_key, 0, _INDEX_MAX - 1)
            await pipe.execute()
        except Exception:
            for _rec, dedup_key in batch:
                if dedup_key:
                    try:
                        await r.delete(dedup_key)
                    except Exception:  # noqa: BLE001 — 回滚尽力
                        pass
            raise
        return len(batch)

    async def aclose(self) -> None:
        """关停兜底：取消定时器并 flush 尾巴（lifespan shutdown 调用）。"""
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        if self._pending:
            try:
                await self.flush()
            except Exception as exc:  # noqa: BLE001 — 关停不外抛
                logger.warning("redis event flush on close failed: {}", exc)
        if self._flush_task is not None and not self._flush_task.done():
            try:
                await self._flush_task
            except Exception:  # noqa: BLE001
                pass

    async def wait(
        self, event_type: str, *, timeout_s: float = 0.0, poll_s: float = 0.01
    ) -> Optional[Dict[str, Any]]:
        if not self._identity.can("event:wait"):
            raise PermissionError("virtual identity lacks event:wait scope")

        r = self._r()
        stream = self._stream_key(event_type)
        deadline = time.time() + max(0.0, timeout_s)
        first = True
        while True:
            entries: Iterable[tuple] = await r.xrange(stream, count=100)
            for eid, fields in entries:
                rec = _record_from_json(str((fields or {}).get("data", "")))
                if rec is None:
                    await r.xdel(stream, eid)  # 损坏条目容忍清理
                    continue
                if rec.is_expired():
                    rec.status = EventStatus.EXPIRED
                    await self._save(rec)
                    await r.xdel(stream, eid)
                    continue
                if rec.status is not EventStatus.PENDING:
                    await r.xdel(stream, eid)
                    continue
                # 原子认领：XDEL 返回 1 才算本 waiter 赢（并发投递互斥）
                if int(await r.xdel(stream, eid)) != 1:
                    continue
                rec.status = EventStatus.DELIVERED
                await self._save(rec)
                return rec.to_dict()
            if not first and time.time() >= deadline:
                return None
            if timeout_s <= 0:
                return None
            first = False
            now = time.time()
            if now >= deadline:
                return None
            await asyncio.sleep(min(poll_s, max(0.0, deadline - now)))

    async def deliver_with_retry(
        self,
        event_id: str,
        handler: Callable[[EventRecord], Awaitable[bool]],
        *,
        backoff_s: float = 0.01,
    ) -> bool:
        rec = await self._load(event_id)
        if rec is None:
            raise KeyError(event_id)
        while rec.attempts <= rec.max_retries:
            if rec.is_expired():
                rec.status = EventStatus.EXPIRED
                await self._save(rec)
                return False
            rec.attempts += 1
            try:
                ok = await handler(rec)
            except Exception as exc:  # noqa: BLE001 — 事件边界必须吞异常转重试
                rec.last_error = str(exc)
                ok = False
            if ok:
                rec.status = EventStatus.DELIVERED
                await self._save(rec)
                return True
            if rec.attempts > rec.max_retries:
                rec.status = EventStatus.FAILED
                await self._save(rec)
                return False
            await asyncio.sleep(backoff_s * (2 ** (rec.attempts - 1)))
            await self._save(rec)
        rec.status = EventStatus.FAILED
        await self._save(rec)
        return False

    # -- 只读查询扩展（鸭子面，非 EventPort 协议成员——ops 查询用） --------

    def get(self, event_id: str) -> Optional[EventRecord]:
        raise NotImplementedError("Redis bus: use await async_get() or ops list_recent")

    async def async_get(self, event_id: str) -> Optional[EventRecord]:
        return await self._load(event_id)

    def pending_count(self, event_type: Optional[str] = None) -> int:
        raise NotImplementedError("Redis bus: use await async_pending_count()")

    async def async_pending_count(self, event_type: Optional[str] = None) -> int:
        types = [event_type] if event_type else await self._all_types()
        total = 0
        for et in types:
            for _, fields in await self._r().xrange(self._stream_key(et), count=10000):
                rec = _record_from_json(str((fields or {}).get("data", "")))
                if (
                    rec is not None
                    and rec.status is EventStatus.PENDING
                    and not rec.is_expired()
                ):
                    total += 1
        return total

    async def list_recent(
        self, event_type: Optional[str] = None, *, limit: int = 50
    ) -> list[Dict[str, Any]]:
        r = self._r()
        ids = await r.lrange(self.index_key, 0, max(0, limit * 4) + 8)
        out: list[Dict[str, Any]] = []
        for eid in reversed(list(ids or [])):  # index 旧→新，倒序取新
            rec = await self._load(str(eid))
            if rec is None:
                continue
            if event_type is not None and rec.event_type != event_type:
                continue
            out.append(rec.to_dict())
            if len(out) >= limit:
                break
        return out

    async def _all_types(self) -> list[str]:
        types: set[str] = set()
        async for key in self._r().scan_iter(match=f"{self._prefix}stream:*"):
            name = str(key)
            types.add(name[len(self._prefix) + len("stream:"):])
        return sorted(types)
