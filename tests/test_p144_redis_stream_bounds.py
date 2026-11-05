"""T10 redis_stream 内存上界验收（p144）。

验收口径，
逐条对应：

- ① 去重键带 TTL：逐条路径的首次 `SET NX` 与**极端竞态续占**那一次都必须是
  `SET NX EX`（漏第二处等于给无上界留后门）；
- ② XADD 带 maxlen 且写明**近似裁剪**（`approximate=True` ⇔ `MAXLEN ~`）：
  逐条写与**批量 pipeline 写**两处都要落，漏一处等于没修；
- ③ 连接池显式上界：`from_url(..., max_connections=...)`；
- ④ Docker 环境实测三键生效读数：本机无容器 ⇒ 带 `integration` 标记恒 skip，
  **本轮未实测**（不许把推断数当读数）。

另覆盖本卡的两个易错点：

- 去重键 TTL 与**记录级** `expires_at`/`is_expired()` 的语义边界（两回事，互不遮蔽）；
- "记录已过期但去重键仍在"与"记录不过期但去重键有上界"两种情形行为可判定。

测试不依赖真实 Redis：一律用记录调用参数的假 client；裁剪相关断言只看**传参**
（桩语义），不假设真 Redis 的裁剪读数。

运行：./.venv/Scripts/python.exe -m pytest tests/test_p144_redis_stream_bounds.py -q
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

import pytest

from agentix.agent.events import redis_stream as rs
from agentix.agent.events.adapter import EventDedupKey, EventRecord, EventStatus
from agentix.agent.events.redis_stream import RedisStreamEventBus
from agentix.config import settings


# ===========================================================================
# 假 client：记录每一次命令的 (op, args, kwargs)
# ===========================================================================


class _FakePipe:
    """pipeline 桩：顺序收集命令与 kwargs（含 maxlen/approximate），execute 时才应用。"""

    def __init__(self, owner: "_FakeRedis") -> None:
        self._owner = owner
        self._ops: List[Tuple[str, tuple, Dict[str, Any]]] = []

    def xadd(self, key: str, fields: Dict[str, Any], **kw):
        self._ops.append(("xadd", (key, fields), kw))
        return self

    def set(self, key: str, value: str, **kw):
        self._ops.append(("set", (key, value), kw))
        return self

    def rpush(self, key: str, value: str, **kw):
        self._ops.append(("rpush", (key, value), kw))
        return self

    def ltrim(self, key: str, start: int, end: int, **kw):
        self._ops.append(("ltrim", (key, start, end), kw))
        return self

    async def execute(self):
        if self._owner.fail_execute:
            raise RuntimeError("connection down")
        self._owner.batches.append(list(self._ops))
        for name, args, kw in self._ops:
            await self._owner._apply(name, args, kw)
        return [True] * len(self._ops)


class _FakeRedis:
    """覆盖 RedisStreamEventBus 命令面的记录型替身。

    `force_nx_conflict` 用来复现 publish 里的极端竞态：首次 `SET NX` 判为冲突
    （返回 falsy）而 `GET` 又读到空 ⇒ 走第二次续占分支。
    """

    def __init__(self, *, force_nx_conflict: bool = False) -> None:
        self.calls: List[Tuple[str, tuple, Dict[str, Any]]] = []
        self.batches: List[List[Tuple[str, tuple, Dict[str, Any]]]] = []
        self.store: Dict[str, str] = {}
        self.streams: Dict[str, List[Tuple[str, Dict[str, str]]]] = {}
        self.lists: Dict[str, List[str]] = {}
        self.index: Dict[str, int] = {}
        self.fail_execute = False
        self._force_nx_conflict = force_nx_conflict
        self._nx_conflict_consumed = False

    # -- 记录 ---------------------------------------------------------------

    def _note(self, op: str, args: tuple, kw: Dict[str, Any]) -> None:
        self.calls.append((op, args, kw))

    def of(self, op: str) -> List[Tuple[tuple, Dict[str, Any]]]:
        return [(a, kw) for name, a, kw in self.calls if name == op]

    def dedup_sets(self) -> List[Tuple[tuple, Dict[str, Any]]]:
        """去重键上的 SET 调用（按键名前缀筛，排除 rec:* 快照写）。"""
        return [(a, kw) for a, kw in self.of("set") if ":dedup:" in str(a[0])]

    # -- 命令面 -------------------------------------------------------------

    async def _apply(self, op: str, args: tuple, kw: Dict[str, Any]):
        if op == "set":
            return await self.set(*args, **kw, _record=False)
        if op == "xadd":
            return self._apply_xadd(args[0], args[1])
        if op == "rpush":
            self.lists.setdefault(args[0], []).append(args[1])
            return len(self.lists[args[0]])
        if op == "ltrim":
            lst = self.lists.get(args[0], [])
            self.lists[args[0]] = lst[args[1] : args[2] + 1]
            return True
        raise AssertionError(f"unsupported pipeline op {op}")

    async def set(self, key: str, value: str, _record: bool = True, **kw):
        if _record:
            self._note("set", (key, value), kw)
        if kw.get("nx") and self._force_nx_conflict and not self._nx_conflict_consumed:
            # 复现"键刚被外部清掉但 SET NX 仍判冲突"的竞态窗口
            self._nx_conflict_consumed = True
            return None
        if kw.get("nx") and key in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key: str) -> Optional[str]:
        return self.store.get(key)

    async def delete(self, *keys: str) -> int:
        n = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                n += 1
        self._note("delete", keys, {})
        return n

    async def xadd(self, key: str, fields: Dict[str, Any], **kw):
        self._note("xadd", (key,), kw)
        return self._apply_xadd(key, fields)

    def _apply_xadd(self, key: str, fields: Dict[str, Any]) -> str:
        self.index[key] = self.index.get(key, 0) + 1
        eid = f"{self.index[key]}-0"
        self.streams.setdefault(key, []).append((eid, dict(fields)))
        return eid

    async def xrange(self, key: str, count: int = 100):
        return list(self.streams.get(key, []))[:count]

    async def xdel(self, key: str, eid: str) -> int:
        entries = self.streams.get(key, [])
        for i, (existing, _f) in enumerate(entries):
            if existing == eid:
                del entries[i]
                return 1
        return 0

    async def rpush(self, key: str, value: str) -> int:
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    async def ltrim(self, key: str, start: int, end: int) -> bool:
        lst = self.lists.get(key, [])
        self.lists[key] = lst[start : end + 1]
        return True

    async def lrange(self, key: str, start: int, end: int) -> List[Any]:
        lst = self.lists.get(key, [])
        return list(lst[start:] if end == -1 else lst[start : end + 1])

    def scan_iter(self, match: str = ""):
        prefix = match[:-1] if match.endswith("*") else match

        async def _gen():
            for k in list(self.streams):
                if k.startswith(prefix):
                    yield k

        return _gen()

    def pipeline(self, transaction: bool = True) -> _FakePipe:
        return _FakePipe(self)


def _bus(**kw: Any) -> Tuple[RedisStreamEventBus, _FakeRedis]:
    fake = _FakeRedis(**kw)
    bus = RedisStreamEventBus(
        prefix=f"agentix:t144:{id(fake):x}:", client=fake
    )
    return bus, fake


# ===========================================================================
# ① 去重键带 TTL（两处 SET NX 都落）
# ===========================================================================


class TestDedupTtl:
    @pytest.mark.asyncio
    async def test_first_set_nx_carries_ex(self):
        bus, fake = _bus()
        eid = await bus.publish("notify", {"n": 1}, event_key="k1")
        sets = fake.dedup_sets()
        assert len(sets) == 1
        (_key, value), kw = sets[0]
        assert kw.get("nx") is True, "去重必须仍是 SET NX（语义未变）"
        assert kw.get("ex") and int(kw["ex"]) > 0, "验收①：去重键必须带 Redis TTL"
        assert value == eid

    @pytest.mark.asyncio
    async def test_race_reoccupy_also_carries_ex(self):
        """竞态续占那一次 SET NX 也必须带 EX：漏一处等于无上界。"""
        bus, fake = _bus(force_nx_conflict=True)
        eid = await bus.publish("notify", {"n": 1}, event_key="k1")
        sets = fake.dedup_sets()
        assert len(sets) == 2, "应走首次 NX 冲突 + 续占两条 SET"
        for (_key, value), kw in sets:
            assert kw.get("nx") is True
            assert kw.get("ex") and int(kw["ex"]) > 0
            assert value == eid

    @pytest.mark.asyncio
    async def test_no_event_key_writes_no_dedup_key(self):
        """无 event_key 时不进去重分支（边界：不该凭空造带 TTL 的键）。"""
        bus, fake = _bus()
        await bus.publish("notify", {"n": 1})
        assert fake.dedup_sets() == []

    @pytest.mark.asyncio
    async def test_ttl_is_bounded_by_constants(self):
        """TTL 恒落在 [_DEDUP_TTL_MIN_S, _DEDUP_TTL_MAX_S]（上界可判读）。

        后两个是**入带值**（返工严重项 1 补的覆盖）：remaining 落在
        [MAX-GRACE, MAX) 时 ceil(remaining)+GRACE 会越过 MAX——旧实现只夹下界
        （如 ttl_s=MAX-1 ⇒ 608399 > 声明上界），原取值集恰好全部绕过该带。
        """
        for ttl_s in (
            0,
            1,
            5,
            3600,
            rs._DEDUP_TTL_MAX_S - 3000,  # 入带：宽限后越界，须被夹回 MAX
            rs._DEDUP_TTL_MAX_S - 1,  # 入带：reviewer 复现值，旧实现返回 608399
            30 * 24 * 3600,
            365 * 24 * 3600,
        ):
            bus, fake = _bus()
            await bus.publish("notify", {"n": ttl_s}, event_key=f"k{ttl_s}", ttl_s=ttl_s)
            ex = int(fake.dedup_sets()[0][1]["ex"])
            assert rs._DEDUP_TTL_MIN_S <= ex <= rs._DEDUP_TTL_MAX_S


# ===========================================================================
# ② XADD 带 maxlen + 近似裁剪（逐条 + 批量 pipeline 两处）
# ===========================================================================


class TestXaddMaxlen:
    @pytest.mark.asyncio
    async def test_single_path_xadd_has_approximate_maxlen(self):
        bus, fake = _bus()
        await bus.publish("cron", {"n": 1})
        xadds = fake.of("xadd")
        assert len(xadds) == 1
        _args, kw = xadds[0]
        assert kw.get("maxlen") == rs._STREAM_MAXLEN
        # 近似裁剪语义必须显式声明（MAXLEN ~）：真 Redis 在节点边界削，实际长度可略大
        assert kw.get("approximate") is True

    @pytest.mark.asyncio
    async def test_batch_pipeline_xadd_has_approximate_maxlen(self, monkeypatch):
        """批量路径（pipeline）同样必须带 maxlen——漏 :275 等于没修。"""
        monkeypatch.setattr(settings, "EVENT_BATCH_MAX_SIZE", 50)
        monkeypatch.setattr(settings, "EVENT_BATCH_FLUSH_MS", 60_000)
        bus, fake = _bus()
        for i in range(3):
            await bus.publish("cron", {"n": i}, event_key=f"k{i}")
        assert fake.batches == []  # 满批未触发，flush 前不落库
        assert await bus.flush() == 3
        batch = fake.batches[0]
        xadd_ops = [op for op in batch if op[0] == "xadd"]
        assert len(xadd_ops) == 3
        for _key, _fields, kw in xadd_ops:
            assert kw.get("maxlen") == rs._STREAM_MAXLEN
            assert kw.get("approximate") is True
        # 批写路径的去重仍是即时 SET NX EX（dedup 不进 pipeline）
        for (_key, _v), kw in fake.dedup_sets():
            assert kw.get("nx") is True and int(kw["ex"]) > 0

    @pytest.mark.asyncio
    async def test_maxlen_keeps_recent_window_readable(self):
        """裁剪界必须 ≥ ops 查询窗（index 上限），并给未来重放留出余量。"""
        assert rs._STREAM_MAXLEN >= rs._INDEX_MAX, (
            "裁剪界小于 ops 查询窗会让 list_recent 稳定读空"
        )
        # T24（SSE 断线重放）接入时的硬约束：裁剪界须 ≥ 重放窗口。
        # 本轮 T24 不开工，用常数下限把这条约束钉住（重放窗口若按小时/天计，
        # 10_000 条量级是下界；改小本值必须同时复核重放窗口口径）。
        assert rs._STREAM_MAXLEN >= 10_000

    @pytest.mark.asyncio
    async def test_trim_params_do_not_change_visible_order(self):
        """桩只记传参、不真裁剪 ⇒ 断言写入顺序与内容不因新增参数改变。"""
        bus, fake = _bus()
        ids = [await bus.publish("cron", {"n": i}) for i in range(3)]
        got = []
        while True:
            item = await bus.wait("cron", timeout_s=0)
            if item is None:
                break
            got.append(item["event_id"])
        assert got == ids
        assert all(kw.get("maxlen") for _a, kw in fake.of("xadd"))


# ===========================================================================
# ③ 连接池显式上界
# ===========================================================================


class TestConnectionPoolBound:
    @pytest.mark.asyncio
    async def test_from_url_receives_max_connections(self, monkeypatch):
        redis_async = pytest.importorskip("redis.asyncio")
        captured: Dict[str, Any] = {}

        def _fake_from_url(url: str, **kw):
            captured["url"] = url
            captured["kw"] = kw
            return _FakeRedis()

        monkeypatch.setattr(redis_async.Redis, "from_url", staticmethod(_fake_from_url))
        bus = RedisStreamEventBus(url="redis://localhost:6379/0", prefix="agentix:t144:")
        bus._r()  # 懒建：首个触达才连库
        assert "max_connections" in captured["kw"], "验收③：必须显式传池上界"
        assert int(captured["kw"]["max_connections"]) == rs._POOL_MAX_CONNECTIONS
        assert captured["kw"]["max_connections"] > 0
        # 既有连接参数不因新增上界而被顺手改动（最小改动）
        assert captured["kw"]["decode_responses"] is True
        assert captured["kw"]["socket_connect_timeout"] == 2

    @pytest.mark.asyncio
    async def test_injected_client_does_not_build_pool(self):
        """注入 client 时不触 from_url（测试面零副作用，构造不连库）。"""
        bus, _fake = _bus()
        assert isinstance(bus._r(), _FakeRedis)


# ===========================================================================
# ④ 去重 TTL 与记录级过期：语义不冲突、行为可判定
# ===========================================================================


def _rec(expires_at: Optional[float]) -> EventRecord:
    return EventRecord(
        event_id="e1",
        event_type="notify",
        payload={},
        dedup_key=EventDedupKey("notify", "k1"),
        occurred_at=0.0,
        received_at=0.0,
        expires_at=expires_at,
    )


def bus_ttl(rec: EventRecord) -> int:
    """去重键 TTL 口径的唯一入口（就是被测实现，不在测试里另算一份）。"""
    return RedisStreamEventBus._dedup_ttl_s(rec)


def now_plus(offset_s: float) -> float:
    """当前时刻 + offset_s（offset 为负 = 过去）。"""
    return time.time() + offset_s


def _expire_stream_entries(fake: _FakeRedis, stream_key: str) -> None:
    """把流内条目的 ``expires_at`` 推到过去（不 sleep：过期只看字段）。

    ``wait`` 读的是**流条目**里的 JSON 快照，改 ``rec:*`` 键不影响它——这两份
    副本的分工正是本卡要写清的口径。
    """
    for i, (eid, fields) in enumerate(list(fake.streams.get(stream_key, []))):
        data = json.loads(fields["data"])
        data["expires_at"] = now_plus(-1)
        entries = fake.streams[stream_key]
        entries[i] = (eid, {"data": json.dumps(data)})


class TestDedupTtlVsRecordExpiry:
    """记录级 `expires_at`（投递侧判 EXPIRED + XDEL）与去重键 Redis TTL 是两回事。"""

    def test_record_without_expiry_gets_capped_ttl(self):
        """记录**不过期**（ttl_s<=0 ⇒ expires_at=None）时去重键仍必须取有限值。"""
        assert bus_ttl(_rec(None)) == rs._DEDUP_TTL_MAX_S

    def test_record_expired_but_dedup_key_still_alive(self):
        """情形一：记录已过期，去重键仍在（TTL ≥ 下界）——两条判定互不遮蔽。"""
        rec = _rec(now_plus(-10))
        assert rec.is_expired() is True, "记录级判定：已过期"
        assert bus_ttl(rec) >= rs._DEDUP_TTL_MIN_S > 0, "去重键仍存活至少一个下界"

    def test_record_alive_but_dedup_ttl_bounded(self):
        """情形二：记录远未到过期，去重键 TTL 有上界 ⇒ 超上界后同 key 不再去重。"""
        far_future = _rec(now_plus(30 * 24 * 3600))  # 30 天后才过期
        assert far_future.is_expired() is False
        ttl = bus_ttl(far_future)
        assert ttl == rs._DEDUP_TTL_MAX_S, "剩余寿命超上界 ⇒ 被上界截断（文档化取舍）"
        assert ttl < 30 * 24 * 3600

    def test_dedup_key_outlives_short_lived_record(self):
        """短寿命记录：去重键 TTL 晚于记录过期 ⇒ 只会空占，不会放开重复处理。"""
        rec = _rec(now_plus(1))  # 1 秒后过期
        ttl = bus_ttl(rec)
        assert ttl > 1
        assert ttl <= rs._DEDUP_TTL_MAX_S

    def test_degenerate_far_future_does_not_raise(self):
        """畸形值（inf）必须落到上界而不是把 publish 打挂（ceil(inf) 会溢出）。"""
        assert bus_ttl(_rec(float("inf"))) == rs._DEDUP_TTL_MAX_S

    @pytest.mark.asyncio
    async def test_duplicate_publish_is_deduped_within_ttl(self):
        """去重键存活期内的二次 publish 返回首次 event_id，且不重复入流。"""
        bus, fake = _bus()
        first = await bus.publish("notify", {"n": 1}, event_key="k1", ttl_s=3600)
        second = await bus.publish("notify", {"n": 1}, event_key="k1", ttl_s=3600)
        assert second == first
        assert len(fake.of("xadd")) == 1

    @pytest.mark.asyncio
    async def test_expired_record_not_delivered_while_dedup_key_alive(self):
        """记录过期只影响投递侧（EXPIRED + XDEL），不影响去重命中。"""
        bus, fake = _bus()
        eid = await bus.publish("notify", {"n": 1}, event_key="k1", ttl_s=3600)
        _expire_stream_entries(fake, bus._stream_key("notify"))
        assert await bus.wait("notify", timeout_s=0) is None
        rec = await bus.async_get(eid)
        assert rec is not None and rec.status is EventStatus.EXPIRED
        # 同一 event_key 仍在去重窗口内 ⇒ 二次 publish 不产生新事件
        assert await bus.publish("notify", {"n": 1}, event_key="k1") == eid


# ===========================================================================
# 验收④：真 Redis（Docker）三键生效读数——本机无容器 ⇒ 恒 skip + integration
# ===========================================================================


def _redis_server_ok() -> bool:
    """探活用**同步**客户端（与 `tests/test_agent_phase23_interaction.py:62-71` 同源）：
    `redis.asyncio.Redis.ping()` 返回协程，不 await 恒为真值 ⇒ 用它探活会把
    "无运行时"误判成"可用"，skip 判据就此失真。
    """
    try:
        import redis as sync_redis

        client = sync_redis.Redis.from_url(
            str(settings.REDIS_URL), socket_connect_timeout=0.5, socket_timeout=0.5
        )
        return bool(client.ping())
    except Exception:  # noqa: BLE001 — 探活失败即无服务器（诚实 skip，不伪造读数）
        return False


_REDIS_OK = _redis_server_ok()


@pytest.mark.integration
@pytest.mark.skipif(
    not _REDIS_OK, reason="Redis 无运行时（诚实 skip，不伪造 baseline）"
)
@pytest.mark.asyncio
async def test_real_redis_bounds_readout():
    """真 Redis 读数：去重键 TTL > 0、流长度受裁剪界约束、池上界可见。

    **本轮（Windows 无容器）未实测**：这条只在带 Redis 运行时（Docker）的环境下
    成立，CI 口径 `-m "not integration"` 会 deselect 它。
    """
    import redis.asyncio as redis_async

    client = redis_async.Redis.from_url(
        str(settings.REDIS_URL), decode_responses=True, socket_connect_timeout=1
    )

    from uuid import uuid4

    maxlen_probe = 20  # 把裁剪界临时压小，真裁剪才可观测
    bus = RedisStreamEventBus(prefix=f"agentix:t144:{uuid4().hex}:", client=client)
    # 池上界必须走**生产路径**（`_r()` 的 from_url），不是本用例自己造的 client
    probe_bus = RedisStreamEventBus(
        url=str(settings.REDIS_URL), prefix="agentix:t144:probe:"
    )
    ids = []
    try:
        original = rs._STREAM_MAXLEN
        rs._STREAM_MAXLEN = maxlen_probe
        try:
            ids = [
                await bus.publish("t144.probe", {"i": i}, event_key=f"k{i}")
                for i in range(120)
            ]
        finally:
            rs._STREAM_MAXLEN = original

        dedup_ttl_ms = await client.pttl(bus._dedup_key(EventDedupKey("t144.probe", "k0")))
        assert 0 < dedup_ttl_ms <= rs._DEDUP_TTL_MAX_S * 1000

        xlen = await client.xlen(bus._stream_key("t144.probe"))
        # 近似裁剪（MAXLEN ~）允许略大于界，但绝不允许 120 条全留
        assert xlen <= maxlen_probe * 3, f"近似裁剪未生效：XLEN={xlen}"
        assert xlen < 120

        pool_max = probe_bus._r().connection_pool.max_connections
        assert int(pool_max) == rs._POOL_MAX_CONNECTIONS, "连接池必须有显式上界"
    finally:
        for eid in ids:
            await client.delete(bus._rec_key(eid))
        await client.delete(bus._stream_key("t144.probe"))
        await client.delete(bus._dedup_key(EventDedupKey("t144.probe", "k0")))
        await client.aclose()
        await probe_bus._r().aclose()
