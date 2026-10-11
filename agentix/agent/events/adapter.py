"""Long-running Event Adapter（Phase 9）。

实现 `domain.ports.agent_core.EventPort`：事件去重、超时、重试、
双时刻（occurred_at 业务侧 vs received_at 接收侧）、虚拟身份凭证边界。

零第三方：in-memory 总线；生产可换 Redis Stream 实现同一 Port。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Optional

__all__ = [
    "EventDedupKey",
    "EventRecord",
    "EventStatus",
    "InMemoryEventBus",
    "VirtualIdentity",
]


class EventStatus(str, Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    EXPIRED = "expired"


@dataclass(frozen=True)
class EventDedupKey:
    """去重键：event_type + 业务侧 event_key（外部系统唯一 ID）。"""

    event_type: str
    event_key: str

    @classmethod
    def make(cls, event_type: str, event_key: Optional[str] = None) -> "EventDedupKey":
        return cls(event_type=event_type, event_key=event_key or "")


@dataclass
class EventRecord:
    event_id: str
    event_type: str
    payload: Dict[str, Any]
    dedup_key: EventDedupKey
    occurred_at: float  # 业务侧时刻（源系统产生）
    received_at: float  # 接收侧时刻（本系统入站）
    status: EventStatus = EventStatus.PENDING
    attempts: int = 0
    max_retries: int = 3
    last_error: str = ""
    expires_at: Optional[float] = None

    @property
    def age_s(self) -> float:
        return time.time() - self.received_at

    def is_expired(self) -> bool:
        return self.expires_at is not None and time.time() > self.expires_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "payload": dict(self.payload),
            "dedup_key": {"event_type": self.dedup_key.event_type,
                          "event_key": self.dedup_key.event_key},
            "occurred_at": self.occurred_at,
            "received_at": self.received_at,
            "status": self.status.value,
            "attempts": self.attempts,
            "max_retries": self.max_retries,
            "last_error": self.last_error,
            "expires_at": self.expires_at,
        }


@dataclass
class VirtualIdentity:
    """虚拟身份边界：事件工具只见 scoped identity，不见平台主凭证。"""

    identity_id: str
    scopes: tuple = ()
    #: 真实凭证**绝不**放入本对象字段（只存外部 secret 引用）
    credential_ref: str = ""

    @classmethod
    def mint(cls, *, scopes: tuple = (), credential_ref: str = "") -> "VirtualIdentity":
        return cls(
            identity_id=f"vid_{uuid.uuid4().hex[:16]}",
            scopes=tuple(scopes),
            credential_ref=credential_ref,
        )

    def can(self, scope: str) -> bool:
        return scope in self.scopes

    def to_public_dict(self) -> Dict[str, Any]:
        return {"identity_id": self.identity_id, "scopes": list(self.scopes)}


class InMemoryEventBus:
    """EventPort 的进程内实现。

    - 去重：同 `EventDedupKey` 二次 publish 返回首次 event_id，不重复入队；
    - 超时：`expires_at` 后 wait 返回 None，状态置 EXPIRED；
    - 重试：deliver 回调失败按指数退避，超过 max_retries → FAILED；
    - 双时刻：record 同时保留 occurred_at / received_at。
    """

    def __init__(self, *, identity: Optional[VirtualIdentity] = None) -> None:
        self._records: Dict[str, EventRecord] = {}
        self._by_dedup: Dict[EventDedupKey, str] = {}
        self._identity = identity or VirtualIdentity.mint(scopes=("event:publish", "event:wait"))

    @property
    def identity(self) -> VirtualIdentity:
        return self._identity

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
        if key in self._by_dedup:
            return self._by_dedup[key]  # 去重：不重复入队

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
        self._records[rec.event_id] = rec
        if key.event_key:
            self._by_dedup[key] = rec.event_id

        return rec.event_id

    async def wait(
        self, event_type: str, *, timeout_s: float = 0.0, poll_s: float = 0.01
    ) -> Optional[Dict[str, Any]]:
        if not self._identity.can("event:wait"):
            raise PermissionError("virtual identity lacks event:wait scope")

        # 非阻塞：立即找一条未过期 pending
        if timeout_s <= 0:
            rec = self._take_next(event_type)
            return rec.to_dict() if rec else None

        deadline = time.time() + timeout_s
        while True:
            rec = self._take_next(event_type)
            if rec is not None:
                return rec.to_dict()
            now = time.time()
            if now >= deadline:
                return None
            await asyncio.sleep(min(poll_s, max(0.0, deadline - now)))

    # -- 内部 -------------------------------------------------------------

    def _take_next(self, event_type: str) -> Optional[EventRecord]:
        for rec in list(self._records.values()):
            if rec.event_type != event_type or rec.status is not EventStatus.PENDING:
                continue
            if rec.is_expired():
                rec.status = EventStatus.EXPIRED
                continue
            rec.status = EventStatus.DELIVERED
            return rec
        return None

    def get(self, event_id: str) -> Optional[EventRecord]:
        return self._records.get(event_id)

    def pending_count(self, event_type: Optional[str] = None) -> int:
        return sum(
            1
            for r in self._records.values()
            if r.status is EventStatus.PENDING
            and (event_type is None or r.event_type == event_type)
        )

    # -- 只读查询扩展（鸭子面，非 EventPort 协议成员——ops 查询用） --------

    def list_recent(
        self, event_type: Optional[str] = None, *, limit: int = 50
    ) -> list[Dict[str, Any]]:
        """按发布顺序倒序返回事件快照（dict），可按类型过滤。

        用 dict 插入序而非 ``received_at`` 排序：Windows 时钟精度可能让同
        tick 内多次 publish 的时间戳打平，插入序才严格单调（= 发布序）。
        """
        recs = [
            r
            for r in reversed(list(self._records.values()))
            if event_type is None or r.event_type == event_type
        ]
        return [r.to_dict() for r in recs[: max(0, limit)]]

    # -- 重试交付（给外部 webhook/consumer 用的辅助面） --------------------

    async def deliver_with_retry(
        self,
        event_id: str,
        handler: Callable[[EventRecord], Awaitable[bool]],
        *,
        backoff_s: float = 0.01,
    ) -> bool:
        rec = self._records.get(event_id)
        if rec is None:
            raise KeyError(event_id)
        while rec.attempts <= rec.max_retries:
            if rec.is_expired():
                rec.status = EventStatus.EXPIRED
                return False
            rec.attempts += 1
            try:
                ok = await handler(rec)
            except Exception as exc:  # noqa: BLE001 — 事件边界必须吞异常转重试
                rec.last_error = str(exc)
                ok = False
            if ok:
                rec.status = EventStatus.DELIVERED
                return True
            if rec.attempts > rec.max_retries:
                rec.status = EventStatus.FAILED
                return False
            await asyncio.sleep(backoff_s * (2 ** (rec.attempts - 1)))
        rec.status = EventStatus.FAILED
        return False

    def to_persistence(self) -> Dict[str, Any]:
        return {
            "records": {k: v.to_dict() for k, v in self._records.items()},
            "dedup": {
                f"{k.event_type}|{k.event_key}": v
                for k, v in self._by_dedup.items()
            },
            "identity": self._identity.to_public_dict()
            | {"credential_ref": self._identity.credential_ref},
        }

    @classmethod
    def from_persistence(
        cls, data: Dict[str, Any], *, identity: Optional[VirtualIdentity] = None
    ) -> "InMemoryEventBus":
        if identity is None:
            saved = data.get("identity") or {}
            if saved.get("identity_id"):
                identity = VirtualIdentity(
                    identity_id=str(saved["identity_id"]),
                    scopes=tuple(saved.get("scopes") or ()),
                    credential_ref=str(saved.get("credential_ref", "")),
                )
        bus = cls(identity=identity)
        for eid, d in (data.get("records") or {}).items():
            dk = d.get("dedup_key") or {}
            rec = EventRecord(
                event_id=d["event_id"],
                event_type=d["event_type"],
                payload=dict(d.get("payload") or {}),
                dedup_key=EventDedupKey(
                    event_type=dk.get("event_type", ""),
                    event_key=dk.get("event_key", ""),
                ),
                occurred_at=float(d.get("occurred_at", 0.0)),
                received_at=float(d.get("received_at", 0.0)),
                status=EventStatus(d.get("status", EventStatus.PENDING.value)),
                attempts=int(d.get("attempts", 0)),
                max_retries=int(d.get("max_retries", 3)),
                last_error=str(d.get("last_error", "")),
                expires_at=d.get("expires_at"),
            )
            bus._records[eid] = rec
            if rec.dedup_key.event_key:
                bus._by_dedup[rec.dedup_key] = eid
        return bus
