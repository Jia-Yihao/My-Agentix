"""RedisStreamEventBus：EventPort 的 Redis Stream 实现（Phase 23，Ch6 §6.2）。

布局（``agentix:events:`` 前缀，全部键名可经 ``prefix`` 覆盖）：

- ``stream:{event_type}`` — XADD 待投递队列；投递成功/过期后 XDEL
  （⇒ 重启后 delivered 不重投，与 Jsonl 的 state 快照语义对齐）；
  XADD 带 ``MAXLEN ~``（**近似裁剪**，界见 ``_STREAM_MAXLEN``）⇒ 单流条目数有上界；
  代价：单一事件类型积压超界且未消费时，流头部**未投递的 PENDING 条目被削掉且
  无告警**，投递承诺在积压态下由 at-least-once 收窄为「有界的 at-most-once」
  （取舍成文与裁决待办见 ``_STREAM_MAXLEN`` 注释块）；
- ``rec:{event_id}`` — 事件快照（STRING json，``EventRecord.to_dict()`` 同形），
  供 ``get``/``list_recent``/``deliver_with_retry`` 读取，投递后仍保留；
- ``dedup:{event_type}:{event_key}`` — SET NX 去重键（跨进程/跨重启唯一，
  同 ``event_key`` 二次 publish 返回首次 ``event_id``）；带 Redis TTL
  （口径见 ``_dedup_ttl_s``/``_DEDUP_TTL_*``，与记录级 ``expires_at`` 是两回事）；
- ``index`` — 最近事件 id 列表（RPUSH + LTRIM ≤2000，``list_recent`` 用）。

**分层决策**（规划 §3 原文倾向 infrastructure，此处落 agent 层，理由记档）：
memory/jsonl 两个 EventPort 实现均居 ``agentix/agent/events/``；全仓无
agent→infrastructure import 先例；domain Protocol 不动、redis 懒 import
（包缺失时模块仍可导入，p40 常绿）；查询面 ``list_recent`` 是鸭子扩展
（非 ``EventPort`` 协议成员）。

无 Redis 诚实失败/诚实 skip：连接错误在命令面**向上传播**（不吞不伪造），
契约测试以 ping 探活决定 skip。

**内存上界（T10）**：去重键带 TTL、XADD 近似裁剪、连接池显式 ``max_connections``
三个口子各给一个可判读的常数上界（取值与理由见 ``_DEDUP_TTL_*`` /
``_STREAM_MAXLEN`` / ``_POOL_MAX_CONNECTIONS``）。裁剪的丢失面（积压超界削掉
未投递条目）与去重侧的截断取舍一样，是内存有界的必然代价，成文见
``_STREAM_MAXLEN`` 注释块。**未收的口**：``rec:*`` 快照仍按
event_id 累积（收它要动记录存活语义，超出本卡边界，已登记为已知限制）。
"""

from __future__ import annotations

import asyncio
import json
import math
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

# -- T10 内存上界三口径（去重 TTL / 流裁剪 / 连接池） ------------------------
# 均为**模块级常量**而非 Settings 键：本轮不新增配置面（`settings.py` 由另一批次
# 独占，且仓内没有语义吻合的既有键可复用——`REDIS_CACHE_EXPIRE`/`REDIS_CACHE_MAX_SIZE`
# 属语义缓存子系统，`EVENT_BATCH_MAX_SIZE` 属批写条数，都不是事件去重/流保留的口径）。
# 后续若可配化，建议落 Settings 并走约定 4 五处同步（见交接报告）。

#: ``dedup:*`` 去重键的 Redis TTL **下界**：再短的记录寿命也要留出去重窗口，
#: 否则上游一次重试还没走完就去重键先没了，等于放开重复处理。
_DEDUP_TTL_MIN_S = 60
#: ``dedup:*`` 去重键的 Redis TTL **上界**（7 天）。取这个数的理由：
#: ① 事件源（webhook/notify/cron）的重复投递窗口以分钟～小时计，7 天远超任何
#:    合理上游重试窗；② 记录**不带** ``expires_at`` 时（``ttl_s <= 0`` 是默认）
#:    去重键本来会永存——这是本卡要收的口子，上界让去重键总量 ≤ 7 天窗口内
#:    出现过的不同 ``event_key`` 数；③ 单键量级 ~（键名 + 32 字节 event_id +
#:    Redis 开销）< 1 KB，10 万 event_key/天 ⇒ 约 7 × 10 万 × 1 KB ≈ 7 MB，
#:    是可判读的常数上界。
_DEDUP_TTL_MAX_S = 7 * 24 * 3600
#: 记录带 ``expires_at`` 时的宽限：让去重键**晚于**记录过期。
#: 取舍方向是刻意的——去重键早于记录失效会放开重复处理（错），晚一点只是空占
#: 几十字节（无害）。注意：若记录剩余寿命超过 ``_DEDUP_TTL_MAX_S``，本值会被上界
#: 截断，此时去重键**会**早于记录失效 ⇒ 后果是同 ``event_key`` 在超出上界后再投
#: 不被去重、当成新事件处理。这是"内存有界"换来的取舍，不是实现遗漏。
_DEDUP_TTL_GRACE_S = 3600

#: XADD 的 ``MAXLEN`` 裁剪上限（**近似裁剪** ``MAXLEN ~``：Redis 在 radix-tree
#: 节点边界削，实际长度可略大于该值；近似是刻意的——精确裁剪每次 XADD 都要遍历
#: 计数，官方推荐近似）。可判读的内存上界：单流 ≤ ``_STREAM_MAXLEN`` 条，单条是
#: 一个 ``data`` 字段装 ``EventRecord.to_dict()`` 的 JSON（千级字节量级），
#: 流键个数 = 事件类型个数（有限枚举，非每会话一键）。
#: **丢失面成文（与 ``:73-77`` 去重侧的登记对称——是内存有界换来的取舍，不是
#: 实现遗漏）**：``wait()`` 从流头部 ``xrange`` 取最旧条目，而 ``MAXLEN ~`` 削的
#: 正是头部 ⇒ 当单一 ``event_type`` 积压超过 ``_STREAM_MAXLEN`` 条且未被消费时，
#: **未投递的 PENDING 条目会被永久削掉且无任何告警**，投递承诺由 at-least-once
#: 收窄为「有界的 at-most-once」（仅积压态如此，非积压路径的投递语义不变）。
#: 这是本卡验收②（XADD 带 maxlen）直接命令的必然代价：给条目数设上界，就等于
#: 让超出上界的条目不再排队等投递。与卡边界「不改 EventPort 对外语义」存在张力
#: （极端积压下的承诺确已变化），在此记档待组织者裁决是否补告警/失败回执面。
#: ⚠ 与 T24（SSE 断线重放）的相互约束：**若将来接入重放，裁剪界须 ≥ 重放窗口**，
#: 否则重放会读不到窗口内的旧条目。T24 本轮不开工（Q10 未决），故取一个足够
#: 宽松、且明显大于 ops 查询窗（``_INDEX_MAX``）的值。
_STREAM_MAXLEN = 10_000

#: 连接池显式上界（``from_url(..., max_connections=...)``）。默认 redis-py 的池
#: 上界是 ``None``（不设限），高并发下连接数随调用数增长 ⇒ 内存/fd 无上界。
#: 取值与仓内既有 redis 客户端风格一致（检索域
#: ``CACHE_REDIS_MAX_CONNECTIONS`` 默认 50）；50 × redis-py 默认读写缓冲（各
#: 64 KB）⇒ 约 6.4 MB 量级，池满时 redis-py 抛 ``ConnectionError``（响亮失败，
#: 不静默排队放大延迟）。
_POOL_MAX_CONNECTIONS = 50


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
                url,
                decode_responses=True,
                socket_connect_timeout=2,
                # 显式池上界：不传则 redis-py 默认不设限（见 _POOL_MAX_CONNECTIONS）
                max_connections=_POOL_MAX_CONNECTIONS,
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

    @staticmethod
    def _dedup_ttl_s(rec: EventRecord) -> int:
        """``dedup:*`` 键的 Redis 存活秒数（口径见 ``_DEDUP_TTL_*`` 注释块）。

        与记录级 ``expires_at``/``is_expired()`` 是**两回事**，别混为一谈：

        - ``expires_at`` 是**记录字段**：投递侧（``wait``/``deliver_with_retry``）
          读到它判定"该事件已无意义"，置 EXPIRED 并 XDEL。它不给任何键设 Redis TTL。
        - 本函数产出的是**去重键在 Redis 里的 TTL**（``SET ... EX``），只决定
          "同 ``event_key`` 多久之内不再被当作新事件"。

        规则：无记录过期 ⇒ 取上界 ``_DEDUP_TTL_MAX_S``（否则去重键永存）；有记录
        过期 ⇒ 剩余寿命 + 宽限（让去重键晚于记录失效），再夹进
        ``[_DEDUP_TTL_MIN_S, _DEDUP_TTL_MAX_S]``。
        """
        if rec.expires_at is None:
            return _DEDUP_TTL_MAX_S
        remaining = float(rec.expires_at) - time.time()
        if remaining >= _DEDUP_TTL_MAX_S:
            # 上界截断（含 inf 之类的畸形值：先比大小再取整，避免 ceil 溢出）
            return _DEDUP_TTL_MAX_S
        ttl = math.ceil(remaining) + _DEDUP_TTL_GRACE_S
        # 双侧夹取：docstring 声明「夹进 [MIN, MAX]」，此前只夹下界——remaining 落在
        # [MAX-GRACE, MAX) 入带时 ceil(remaining)+GRACE 会越过 MAX（如 ttl_s=604799
        # ⇒ 608399），:67 的「总量 ≤ 7 天窗口」论证在该带失效。:218 的早截仍保留
        # （它兜 inf 等畸形值的 ceil 溢出，是本夹取覆盖不到的路径）。
        return min(_DEDUP_TTL_MAX_S, max(_DEDUP_TTL_MIN_S, int(ttl)))

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
            # SET NX 直接占真实 id（无空串占位窗口）：输者读到的必是赢家 id。
            # 带 EX：去重键原本永存 ⇒ dedup:* 总量无上界（T10）；TTL 取值口径与
            # 记录级 expires_at 的关系见 _dedup_ttl_s()。
            dedup_ttl = self._dedup_ttl_s(rec)
            won = await r.set(
                self._dedup_key(key), rec.event_id, nx=True, ex=dedup_ttl
            )
            if not won:
                existing = await r.get(self._dedup_key(key))
                if existing:
                    return str(existing)  # 去重：返回首次 event_id
                # 极端竞态（键被外部清掉）：以本记录续占，避免返回空。
                # 同样带 EX——这条路径漏带 TTL 就等于给无上界留了后门。
                await r.set(
                    self._dedup_key(key), rec.event_id, nx=True, ex=dedup_ttl
                )
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
                # 近似裁剪（MAXLEN ~）：不传 maxlen 则流只增不减 ⇒ 内存无上界。
                # 逐条写与下面的批写路径必须带同样的裁剪参数，漏一处等于没修。
                await r.xadd(
                    self._stream_key(event_type),
                    {"data": _record_to_json(rec)},
                    maxlen=_STREAM_MAXLEN,
                    approximate=True,
                )
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
                    # 与逐条写路径同一裁剪口径（近似裁剪，见 _STREAM_MAXLEN）
                    maxlen=_STREAM_MAXLEN,
                    approximate=True,
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
