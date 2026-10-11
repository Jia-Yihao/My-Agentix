"""Phase 23：交互二期（notify_user 出站通知 + redis_stream + 事件矩阵）。

把规划 §3 Phase 23 的 DoD 逐条钉死：

1. **notify_user flag-off 零注册**：声明清单/registry/handler 三面皆无；
2. **flag-on 落事件 + ops 可查 + MEDIUM 触 HITL**：执行 → EventPort
   ``user_notification`` → ``GET /ops/events`` 查到；声明 L1 → MEDIUM，
   gate-on 时 ``review_required`` 拦截（出站即副作用，不得绕闸）；
3. **redis_stream 过同一契约测试**：三 backend 参数化（memory/jsonl/redis），
   无 Redis 运行时**诚实 skip**（不伪造）；backend 分派构造不连库；
4. **事件矩阵去重/超时/取消/并发各 ≥1**（跨 backend 参数化）。

全局单例（声明缓存 / executor / event port）在夹具里成对复位，
与 Phase 21 测试同款范式。
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from pathlib import Path

import pytest

from agentix.agent.events.adapter import InMemoryEventBus
from agentix.agent.events.jsonl import JsonlEventBus
from agentix.agent.events.redis_stream import RedisStreamEventBus
from agentix.agent.events.port import build_event_port, get_event_port, reset_event_port
from agentix.policies.hitl.hitl_bridge import HitlBridge
from agentix.policies.hitl.sidecar import ReviewerSidecar
from agentix.application.services.notify_bridge import install_notify_user_tool
from agentix.tools import registry_init
from agentix.tools.definitions import registry as tool_registry
from agentix.tools.gateway.executor import (
    ToolExecutor,
    get_tool_executor,
    reset_tool_executor,
)
from agentix.tools.gateway.human_review import (
    HumanReviewGate,
    RiskLevel,
    declared_level_for,
    requires_human_review,
)
from agentix.tools.adapters.notify_user import (
    MAX_MESSAGE_CHARS,
    NOTIFY_PRIORITIES,
    NOTIFY_USER_EVENT_TYPE,
    build_notify_user_spec,
    validate_notify_args,
)
from agentix.config.settings import settings
from agentix.interfaces.http.ops_router import list_events

# ---------------------------------------------------------------------------
# Redis 可用性（诚实 skip：探活失败 = 无运行时，绝不伪造）
# ---------------------------------------------------------------------------


def _redis_server_ok() -> bool:
    try:
        import redis as sync_redis

        client = sync_redis.Redis.from_url(
            str(settings.REDIS_URL), socket_connect_timeout=0.5, socket_timeout=0.5
        )
        return bool(client.ping())
    except Exception:  # noqa: BLE001 — 探活失败即无服务器
        return False


_REDIS_OK = _redis_server_ok()
_REDIS_PARAM = pytest.param(
    "redis",
    marks=pytest.mark.skipif(
        not _REDIS_OK, reason="Redis 无运行时（诚实 skip，不伪造 baseline）"
    ),
)
#: 契约/矩阵参数化面（memory/jsonl 恒跑；redis 探活失败自动 skip）
BACKENDS = ["memory", "jsonl", _REDIS_PARAM]


def _make_bus(kind: str, tmp_path: Path, *, prefix: str | None = None):
    if kind == "memory":
        return InMemoryEventBus()
    if kind == "jsonl":
        return JsonlEventBus(tmp_path / "events.jsonl")
    if kind == "redis":
        # 唯一前缀：并发测试不串键；客户端懒建（构造不连库）
        return RedisStreamEventBus(prefix=prefix or f"agentix:test:{uuid.uuid4().hex}:")
    raise AssertionError(kind)


async def _maybe_await(value):
    return await value if inspect.isawaitable(value) else value


# ---------------------------------------------------------------------------
# 全局单例清理
# ---------------------------------------------------------------------------


def _reset_declarations() -> None:
    """清声明缓存 + 摘掉 flag-on 时挂进全局 registry 的 notify_user。"""
    registry_init._DECLARATIONS.clear()
    tool_registry.delete_tool("notify_user")


@pytest.fixture
def notify_on(monkeypatch: pytest.MonkeyPatch):
    """flag-on：声明/executor/事件端口三单例复位，测试后成对复原。"""
    monkeypatch.setattr(settings, "NOTIFY_USER_ENABLED", True)
    _reset_declarations()
    reset_tool_executor()
    reset_event_port()
    yield
    _reset_declarations()
    reset_tool_executor()
    reset_event_port()


@pytest.fixture
def notify_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "NOTIFY_USER_ENABLED", False)
    _reset_declarations()
    reset_tool_executor()
    reset_event_port()
    yield
    _reset_declarations()
    reset_tool_executor()
    reset_event_port()


# ---------------------------------------------------------------------------
# 1. flag-off 零注册 / flag-on 注册面（DoD 1、2 前半）
# ---------------------------------------------------------------------------


class TestNotifyFlagGate:
    def test_default_flag_is_off(self):
        assert settings.NOTIFY_USER_ENABLED is False

    def test_flag_off_zero_registration(self, notify_off):
        registry_init.ensure_registered()
        declared = {spec.name for spec, _cls in registry_init._get_declarations()}
        assert "notify_user" not in declared

        install_notify_user_tool()
        assert not tool_registry.has_tool("notify_user")
        assert not get_tool_executor().has_handler("notify_user")

    def test_flag_on_registers_spec_and_handler(self, notify_on):
        registry_init.ensure_registered()
        declared = {
            spec.name: spec
            for spec, _cls in registry_init._get_declarations()
        }
        spec = declared["notify_user"]
        assert spec.risk_level == "L1"
        assert spec.side_effect is True
        assert "interaction" in (spec.capabilities or ())

        install_notify_user_tool()
        assert tool_registry.has_tool("notify_user")
        assert get_tool_executor().has_handler("notify_user")

    def test_spec_mapping_l1_to_medium_and_word_list(self):
        spec = build_notify_user_spec()
        assert declared_level_for(spec.risk_level) is RiskLevel.MEDIUM
        gate = HumanReviewGate(enabled=True)
        # 词表（notify）∪ 声明级（L1→MEDIUM）双路都判 MEDIUM → 触发审查
        assert requires_human_review(
            spec.name,
            {"channel": "in_app", "message": "done"},
            gate=gate,
            declared_risk_level=spec.risk_level,
        )


# ---------------------------------------------------------------------------
# 2. 参数校验（fail-fast + 规范化）
# ---------------------------------------------------------------------------


class TestNotifyValidation:
    def test_normalization(self):
        out = validate_notify_args(
            {"channel": "  in_app ", "message": "hi\x00 there", "priority": "HIGH"}
        )
        assert out == {
            "channel": "in_app",
            "message": "hi there",
            "priority": "high",
        }

    @pytest.mark.parametrize(
        "args",
        [
            {"channel": "", "message": "x"},
            {"channel": "in_app", "message": ""},
            {"channel": "in_app", "message": "x", "priority": "urgent+"},
            {"channel": "c" * 65, "message": "x"},
        ],
    )
    def test_rejects_invalid(self, args):
        with pytest.raises(ValueError):
            validate_notify_args(args)

    def test_rejects_oversized_message(self):
        with pytest.raises(ValueError):
            validate_notify_args(
                {"channel": "in_app", "message": "m" * (MAX_MESSAGE_CHARS + 1)}
            )

    def test_priority_enum_documented(self):
        assert NOTIFY_PRIORITIES == ("low", "normal", "high", "urgent")


# ---------------------------------------------------------------------------
# 3. 执行 → 落事件 → ops 可查（DoD 2 后半）
# ---------------------------------------------------------------------------


class TestNotifyPublishAndOps:
    @pytest.mark.asyncio
    async def test_execute_records_user_notification(self, notify_on):
        registry_init.ensure_registered()
        install_notify_user_tool()

        result = await get_tool_executor().execute(
            "notify_user",
            {"channel": "in_app", "message": "long task done", "priority": "high"},
        )
        assert result.success, result.error
        assert result.data["status"] == "queued"
        assert result.data["event_type"] == NOTIFY_USER_EVENT_TYPE

        port = get_event_port()
        recent = port.list_recent(NOTIFY_USER_EVENT_TYPE)
        assert len(recent) == 1
        payload = recent[0]["payload"]
        assert payload["channel"] == "in_app"
        assert payload["message"] == "long task done"
        assert payload["priority"] == "high"
        assert "trace_id" in payload

        # 消费面：wait 命中后不再重复投递
        got = await port.wait(NOTIFY_USER_EVENT_TYPE, timeout_s=0)
        assert got is not None and got["event_id"] == recent[0]["event_id"]
        assert port.pending_count(NOTIFY_USER_EVENT_TYPE) == 0

    @pytest.mark.asyncio
    async def test_execute_invalid_args_rejected(self, notify_on):
        registry_init.ensure_registered()
        install_notify_user_tool()

        result = await get_tool_executor().execute(
            "notify_user", {"channel": "", "message": "x"}
        )
        assert not result.success
        assert "channel" in (result.error or "")

    @pytest.mark.asyncio
    async def test_ops_events_endpoint_lists_and_filters(self, notify_on):
        registry_init.ensure_registered()
        install_notify_user_tool()

        ok = await get_tool_executor().execute(
            "notify_user", {"channel": "in_app", "message": "n1"}
        )
        assert ok.success

        listed = await list_events(event_type=NOTIFY_USER_EVENT_TYPE, limit=10)
        assert any(
            item["payload"].get("message") == "n1" for item in listed
        )
        empty = await list_events(event_type="no_such_type", limit=10)
        assert empty == []


# ---------------------------------------------------------------------------
# 4. MEDIUM 触 HITL 闸（出站即副作用，不得绕闸——DoD 2）
# ---------------------------------------------------------------------------


class _AllowPolicy:
    def __init__(self, max_risk_level: str = "L3") -> None:
        self.max_risk_level = max_risk_level


class _Spy:
    def __init__(self) -> None:
        self.calls: list = []

    async def __call__(self, args):
        self.calls.append(dict(args))
        return {"ok": True}


class TestNotifyHitsHITLGate:
    @pytest.mark.asyncio
    async def test_medium_risk_blocked_as_review_required(self, notify_on):
        registry_init.ensure_registered()
        spec = next(
            s for s, _ in registry_init._get_declarations() if s.name == "notify_user"
        )
        gate = HumanReviewGate(enabled=True)
        bridge = HitlBridge(gate=gate, sidecar=ReviewerSidecar())
        ex = ToolExecutor(
            policy=_AllowPolicy(),
            review_gate=gate,
            review_blocked_hook=bridge.open_from_gate,
        )
        spy = _Spy()
        ex.register_handler("notify_user", spy, spec=spec)

        result = await ex.execute(
            "notify_user", {"channel": "email", "message": "ping user"}
        )
        assert not result.success
        assert result.metadata.get("reason") == "review_required"
        assert "request_id" in result.metadata
        assert spy.calls == []  # 审批前 handler 绝不执行（出站不预跑）


# ---------------------------------------------------------------------------
# 5. EventPort backend 分派（23.2：redis_stream 入 build_event_port）
# ---------------------------------------------------------------------------


class TestBackendDispatch:
    def test_redis_stream_backend_constructs_without_connecting(self, monkeypatch):
        monkeypatch.setattr(settings, "EVENT_PORT_BACKEND", "redis_stream")
        port = build_event_port()
        assert isinstance(port, RedisStreamEventBus)  # 构造不连库（懒客户端）

    def test_unknown_backend_still_falls_back_to_memory(self, monkeypatch):
        monkeypatch.setattr(settings, "EVENT_PORT_BACKEND", "bogus")
        assert isinstance(build_event_port(), InMemoryEventBus)

    @pytest.mark.asyncio
    async def test_redis_bus_connection_error_propagates(self):
        """无服务器时诚实失败：连接错误向上传播，不静默伪造成功。"""
        if _REDIS_OK:
            pytest.skip("本机有 Redis，跳过无服务器错误传播用例")
        bus = RedisStreamEventBus(
            prefix=f"agentix:test:{uuid.uuid4().hex}:", url=settings.REDIS_URL
        )
        with pytest.raises(Exception):  # noqa: B017 — 连接族异常类型随驱动版本
            await bus.publish("t", {"n": 1})


# ---------------------------------------------------------------------------
# 6. 跨 backend 契约（同一测试面跑 memory/jsonl/redis——DoD 3）
# ---------------------------------------------------------------------------


class TestBusContract:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", BACKENDS)
    async def test_publish_wait_once(self, kind, tmp_path):
        bus = _make_bus(kind, tmp_path)
        eid = await bus.publish("t", {"n": 1})
        got = await bus.wait("t", timeout_s=0)
        assert got is not None and got["event_id"] == eid
        assert await bus.wait("t", timeout_s=0) is None  # delivered 不重投

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", BACKENDS)
    async def test_dedup_same_key_same_id(self, kind, tmp_path):
        bus = _make_bus(kind, tmp_path)
        a = await bus.publish("t", {"n": 1}, event_key="K")
        b = await bus.publish("t", {"n": 2}, event_key="K")
        assert a == b
        assert await bus.wait("t", timeout_s=0) is not None
        assert await bus.wait("t", timeout_s=0) is None  # 只入队一次

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", BACKENDS)
    async def test_timeout_returns_none(self, kind, tmp_path):
        bus = _make_bus(kind, tmp_path)
        start = time.monotonic()
        assert await bus.wait("nothing", timeout_s=0.05, poll_s=0.005) is None
        assert 0.04 <= time.monotonic() - start < 5.0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", BACKENDS)
    async def test_ttl_expiry_not_delivered(self, kind, tmp_path):
        bus = _make_bus(kind, tmp_path)
        await bus.publish("t", {"n": 1}, ttl_s=0.01)
        await asyncio.sleep(0.03)
        assert await bus.wait("t", timeout_s=0) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", BACKENDS)
    async def test_list_recent_filter_and_order(self, kind, tmp_path):
        bus = _make_bus(kind, tmp_path)
        await bus.publish("a", {"i": 1})
        await bus.publish("b", {"i": 2})
        await bus.publish("a", {"i": 3})
        recent = await _maybe_await(bus.list_recent("a", limit=10))
        assert [item["payload"]["i"] for item in recent] == [3, 1]


# ---------------------------------------------------------------------------
# 7. 事件矩阵：去重/超时/取消/并发各 ≥1（DoD 4）
# ---------------------------------------------------------------------------


class TestEventMatrix:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", BACKENDS)
    async def test_matrix_dedup(self, kind, tmp_path):
        """去重：同 event_key 并发重发 → 同 id、单次投递。"""
        bus = _make_bus(kind, tmp_path)
        ids = await asyncio.gather(
            *(bus.publish("t", {"i": i}, event_key="SAME") for i in range(5))
        )
        assert len(set(ids)) == 1
        assert await bus.wait("t", timeout_s=0) is not None
        assert await bus.wait("t", timeout_s=0) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", BACKENDS)
    async def test_matrix_timeout(self, kind, tmp_path):
        """超时：等不到事件按期返回 None（不挂死、不提前返回）。"""
        bus = _make_bus(kind, tmp_path)
        start = time.monotonic()
        assert await bus.wait("t", timeout_s=0.1, poll_s=0.005) is None
        elapsed = time.monotonic() - start
        assert 0.08 <= elapsed < 5.0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", BACKENDS)
    async def test_matrix_cancel_then_reusable(self, kind, tmp_path):
        """取消：等待中任务被 cancel 后，总线仍可用（无残留锁/毒化）。"""
        bus = _make_bus(kind, tmp_path)
        waiter = asyncio.ensure_future(bus.wait("t", timeout_s=5.0, poll_s=0.01))
        await asyncio.sleep(0.05)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert waiter.cancelled()

        eid = await bus.publish("t", {"after": True})
        got = await bus.wait("t", timeout_s=0.2, poll_s=0.005)
        assert got is not None and got["event_id"] == eid

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", BACKENDS)
    async def test_matrix_concurrent_exactly_one_winner(self, kind, tmp_path):
        """并发投递：5 个 waiter 抢 1 条事件 → 恰好 1 命中、4 超时。"""
        bus = _make_bus(kind, tmp_path)
        waiters = [
            asyncio.ensure_future(bus.wait("t", timeout_s=0.4, poll_s=0.005))
            for _ in range(5)
        ]
        await asyncio.sleep(0.05)
        await bus.publish("t", {"only": 1})
        results = await asyncio.gather(*waiters)
        hits = [r for r in results if r is not None]
        assert len(hits) == 1
        assert sum(1 for r in results if r is None) == 4

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["jsonl", _REDIS_PARAM])
    async def test_matrix_cross_restart_dedup_and_no_redeliver(self, kind, tmp_path):
        """跨重启（同路径/前缀重建实例）：dedup 保留、delivered 不重投。"""
        prefix = f"agentix:test:{uuid.uuid4().hex}:" if kind == "redis" else None
        bus1 = _make_bus(kind, tmp_path, prefix=prefix)
        eid = await bus1.publish("t", {"n": 1}, event_key="K")
        first = await bus1.wait("t", timeout_s=0)
        assert first is not None and first["event_id"] == eid

        bus2 = _make_bus(kind, tmp_path, prefix=prefix)
        assert await bus2.wait("t", timeout_s=0) is None  # 不重投
        eid2 = await bus2.publish("t", {"n": 1}, event_key="K")
        assert eid2 == eid  # 跨重启去重
