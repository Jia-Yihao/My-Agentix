"""Phase 9 — Event Adapter：双时钟、去重、超时、重试、虚拟身份边界。"""

from __future__ import annotations

import asyncio

import pytest

from agentix.agent.events import (
    EventDedupKey,
    EventStatus,
    InMemoryEventBus,
    VirtualIdentity,
)
from agentix.runtime.lifecycle import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    LifecycleManager,
)
from agentix.runtime.state import AgentState, AgentStatus


class TestDualClock:
    @pytest.mark.asyncio
    async def test_occurred_and_received_are_distinct(self):
        bus = InMemoryEventBus()
        occurred = 1_700_000_000.0
        eid = await bus.publish(
            "payment.settled", {"amount": 1}, occurred_at=occurred, event_key="pay-1"
        )
        rec = bus.get(eid)
        assert rec is not None
        assert rec.occurred_at == occurred
        assert rec.received_at > 0
        # 业务侧可早于接收侧（异步入站）
        assert rec.received_at >= rec.occurred_at or rec.occurred_at > 0

    @pytest.mark.asyncio
    async def test_default_occurred_is_receive_time(self):
        bus = InMemoryEventBus()
        eid = await bus.publish("t", {})
        rec = bus.get(eid)
        assert rec is not None
        assert abs(rec.occurred_at - rec.received_at) < 0.05


class TestDedup:
    @pytest.mark.asyncio
    async def test_same_event_key_returns_same_id(self):
        bus = InMemoryEventBus()
        a = await bus.publish("order.created", {"id": 1}, event_key="ord-9")
        b = await bus.publish("order.created", {"id": 2}, event_key="ord-9")
        assert a == b
        assert bus.pending_count("order.created") == 1

    @pytest.mark.asyncio
    async def test_different_keys_do_not_collide(self):
        bus = InMemoryEventBus()
        a = await bus.publish("x", {}, event_key="k1")
        b = await bus.publish("x", {}, event_key="k2")
        assert a != b

    @pytest.mark.asyncio
    async def test_dedup_key_helper(self):
        k = EventDedupKey.make("t", None)
        assert k.event_key == ""
        assert EventDedupKey.make("t") == k


class TestPersistence:
    """to_persistence 必须可 JSON 序列化（历史 bug：tuple 键直接炸 json.dumps）。"""

    @pytest.mark.asyncio
    async def test_to_persistence_is_json_serializable(self):
        import json

        bus = InMemoryEventBus()
        await bus.publish("order.created", {"id": 1}, event_key="ord-9")
        await bus.publish("order.paid", {"id": 2})  # 无 event_key 不进 dedup
        blob = json.dumps(bus.to_persistence())  # 不得 TypeError
        assert "ord-9" in blob

    @pytest.mark.asyncio
    async def test_from_persistence_roundtrip_keeps_dedup_and_status(self):
        import json

        bus = InMemoryEventBus()
        eid = await bus.publish(
            "pay.settled", {"amount": 9}, event_key="pay-1", occurred_at=1.5
        )
        restored = InMemoryEventBus.from_persistence(
            json.loads(json.dumps(bus.to_persistence()))
        )
        assert restored.pending_count("pay.settled") == 1
        rec = restored.get(eid)
        assert rec is not None
        assert rec.occurred_at == 1.5
        # 去重表恢复：同 key 再 publish 返回同一 id
        again = await restored.publish("pay.settled", {"amount": 1}, event_key="pay-1")
        assert again == eid

    @pytest.mark.asyncio
    async def test_from_persistence_restores_identity(self):
        """持久化必须带回 identity（id/scopes/credential_ref），不得静默铸新身份。

        修复前：to_persistence 写了 identity，from_persistence 却忽略它，
        每次恢复都 VirtualIdentity.mint()——重启后 event_id 与 scopes 全丢。
        """
        import json

        original_id = "vid_fixed_roundtrip"
        bus = InMemoryEventBus(
            identity=VirtualIdentity(
                identity_id=original_id,
                scopes=("event:publish",),
                credential_ref="sec://ref",
            )
        )
        assert not bus.identity.can("event:wait")
        await bus.publish("t", {"n": 1}, event_key="k")

        restored = InMemoryEventBus.from_persistence(
            json.loads(json.dumps(bus.to_persistence()))
        )
        assert restored.identity.identity_id == original_id
        assert restored.identity.credential_ref == "sec://ref"
        assert restored.identity.can("event:publish")
        assert not restored.identity.can("event:wait")
        # 显式 identity 参数仍优先于 blob
        override = VirtualIdentity.mint(scopes=())
        restored2 = InMemoryEventBus.from_persistence(
            bus.to_persistence(), identity=override
        )
        assert restored2.identity is override


class TestTimeoutExpiry:
    @pytest.mark.asyncio
    async def test_expired_event_not_delivered(self):
        bus = InMemoryEventBus()
        eid = await bus.publish("late", {}, ttl_s=0.001)
        await asyncio.sleep(0.01)
        out = await bus.wait("late", timeout_s=0)
        assert out is None
        rec = bus.get(eid)
        assert rec is not None
        assert rec.status is EventStatus.EXPIRED

    @pytest.mark.asyncio
    async def test_wait_timeout_returns_none(self):
        bus = InMemoryEventBus()
        out = await bus.wait("never", timeout_s=0.02, poll_s=0.005)
        assert out is None

    @pytest.mark.asyncio
    async def test_wait_consumes_pending(self):
        bus = InMemoryEventBus()
        await bus.publish("go", {"n": 1})
        out = await bus.wait("go", timeout_s=0)
        assert out is not None
        assert out["payload"]["n"] == 1
        assert bus.pending_count("go") == 0


class TestRetry:
    @pytest.mark.asyncio
    async def test_handler_fail_then_succeed(self):
        bus = InMemoryEventBus()
        eid = await bus.publish("r", {})
        calls = {"n": 0}

        async def flaky(rec):
            calls["n"] += 1
            return calls["n"] >= 3

        ok = await bus.deliver_with_retry(eid, flaky, backoff_s=0.001)
        assert ok
        assert calls["n"] == 3
        assert bus.get(eid).status is EventStatus.DELIVERED

    @pytest.mark.asyncio
    async def test_exhaust_retries_marks_failed(self):
        bus = InMemoryEventBus()
        eid = await bus.publish("f", {}, max_retries=2)

        async def always_fail(rec):
            return False

        ok = await bus.deliver_with_retry(eid, always_fail, backoff_s=0.001)
        assert not ok
        rec = bus.get(eid)
        assert rec.status is EventStatus.FAILED
        assert rec.attempts == 3  # initial + 2 retries

    @pytest.mark.asyncio
    async def test_handler_exception_counts_as_failure(self):
        bus = InMemoryEventBus()
        eid = await bus.publish("e", {}, max_retries=0)

        async def boom(rec):
            raise RuntimeError("x")

        ok = await bus.deliver_with_retry(eid, boom, backoff_s=0.001)
        assert not ok
        assert "x" in bus.get(eid).last_error


class TestVirtualIdentityBoundary:
    @pytest.mark.asyncio
    async def test_publish_requires_scope(self):
        vid = VirtualIdentity.mint(scopes=())
        bus = InMemoryEventBus(identity=vid)
        with pytest.raises(PermissionError):
            await bus.publish("t", {})

    @pytest.mark.asyncio
    async def test_wait_requires_scope(self):
        vid = VirtualIdentity.mint(scopes=("event:publish",))
        bus = InMemoryEventBus(identity=vid)
        with pytest.raises(PermissionError):
            await bus.wait("t")

    @pytest.mark.asyncio
    async def test_public_dict_hides_credential_ref(self):
        vid = VirtualIdentity.mint(scopes=("event:publish",), credential_ref="sec://key")
        pub = vid.to_public_dict()
        assert "credential_ref" not in pub
        assert vid.can("event:publish")
        assert not vid.can("event:wait")

    @pytest.mark.asyncio
    async def test_full_roundtrip_with_scoped_identity(self):
        bus = InMemoryEventBus(
            identity=VirtualIdentity.mint(scopes=("event:publish", "event:wait"))
        )
        await bus.publish("ok", {"v": 1}, event_key="e1")
        out = await bus.wait("ok", timeout_s=0)
        assert out["payload"]["v"] == 1


class TestStatusEnumExtension:
    def test_new_statuses_exist(self):
        assert AgentStatus.WAITING_APPROVAL.value == "waiting_approval"
        assert AgentStatus.PAUSED.value == "paused"
        assert AgentStatus.WAITING_EXTERNAL_EVENT.value == "waiting_external_event"

    def test_waiting_external_event_legal_path(self):
        st = AgentState(run_id="r-wee")
        LifecycleManager.transition(st, AgentStatus.RUNNING)
        LifecycleManager.transition(st, AgentStatus.EXECUTING)
        LifecycleManager.transition(st, AgentStatus.WAITING_EXTERNAL_EVENT)
        LifecycleManager.transition(st, AgentStatus.EXECUTING)
        assert st.status is AgentStatus.EXECUTING

    def test_waiting_approval_legal_path(self):
        st = AgentState(run_id="r-wa")
        LifecycleManager.transition(st, AgentStatus.RUNNING)
        LifecycleManager.transition(st, AgentStatus.WAITING_APPROVAL)
        LifecycleManager.transition(st, AgentStatus.RUNNING)
        assert st.status is AgentStatus.RUNNING

    def test_illegal_from_terminal(self):
        st = AgentState(run_id="r-term")
        LifecycleManager.transition(st, AgentStatus.RUNNING)
        LifecycleManager.transition(st, AgentStatus.COMPLETED)
        with pytest.raises(InvalidTransitionError):
            LifecycleManager.transition(st, AgentStatus.WAITING_EXTERNAL_EVENT)

    def test_all_statuses_have_transition_entries(self):
        for status in AgentStatus:
            assert status in ALLOWED_TRANSITIONS
