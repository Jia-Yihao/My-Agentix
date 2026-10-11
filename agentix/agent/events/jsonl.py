"""JsonlEventBus：EventPort 的 JSONL 落盘实现（Phase 20，决策：JSONL 先行）。

结构 = :class:`InMemoryEventBus`（去重/过期/重试语义完全复用）+ append-only
事件日志：

- ``publish`` 新事件 → 追加一行 ``{"op": "publish", ...EventRecord.to_dict()}``；
- ``wait`` 消费成功 / ``deliver_with_retry`` 终态 → 追加 ``{"op": "state", ...}``
  快照行（末次快照生效）；
- 构造时**按序回放**日志：最后一条快照 = 记录终态 → 重启后 pending 仍可被
  ``wait`` 到、已 delivered 不重复投递、同 ``event_key`` 重发仍命中去重；
- 损坏行（进程中断的半行）逐行容忍跳过。

单进程写者假设：多进程并发写同一日志 = 另期 Redis Stream（决策已拍板）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Set

from agentix.agent.events.adapter import EventRecord, InMemoryEventBus, VirtualIdentity

__all__ = ["JsonlEventBus", "DEFAULT_EVENT_LOG_PATH"]

#: 默认事件日志路径（cwd 相对；本批决策零额外新键，代码级可覆盖）
DEFAULT_EVENT_LOG_PATH = Path("events") / "event_log.jsonl"


class JsonlEventBus:
    """EventPort = 进程内总线 + append-only JSONL 日志（语义见模块 docstring）。"""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        identity: Optional[VirtualIdentity] = None,
    ) -> None:
        self._path = Path(path) if path is not None else DEFAULT_EVENT_LOG_PATH
        self._bus = InMemoryEventBus(identity=identity)
        self._logged: Set[str] = set()
        self._restore(identity)

    # -- 恢复 -------------------------------------------------------------

    def _restore(self, identity: Optional[VirtualIdentity]) -> None:
        if not self._path.exists():
            return
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return
        latest: Dict[str, Dict[str, Any]] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # 尾部半行（进程中断）容忍
            if not isinstance(entry, dict):
                continue
            eid = entry.get("event_id")
            if not eid or "event_type" not in entry:
                continue
            latest[str(eid)] = entry  # 末次快照生效（publish/state 同形）
        if not latest:
            return
        self._bus = InMemoryEventBus.from_persistence({"records": latest}, identity=identity)
        self._logged = set(latest)

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
        event_id = await self._bus.publish(
            event_type,
            payload,
            event_key=event_key,
            occurred_at=occurred_at,
            ttl_s=ttl_s,
            max_retries=max_retries,
        )
        if event_id not in self._logged:
            rec = self._bus.get(event_id)
            if rec is not None:
                self._append("publish", rec)
        return event_id

    async def wait(
        self, event_type: str, *, timeout_s: float = 0.0, poll_s: float = 0.01
    ) -> Optional[Dict[str, Any]]:
        result = await self._bus.wait(event_type, timeout_s=timeout_s, poll_s=poll_s)
        if result is not None:
            rec = self._bus.get(str(result.get("event_id", "")))
            if rec is not None:
                self._append("state", rec)  # delivered 快照 → 重启不重投
        return result

    async def deliver_with_retry(self, event_id: str, handler, *, backoff_s: float = 0.01) -> bool:
        try:
            ok = await self._bus.deliver_with_retry(event_id, handler, backoff_s=backoff_s)
        finally:
            rec = self._bus.get(event_id)
            if rec is not None:
                self._append("state", rec)
        return ok

    # -- 便捷透传 ---------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def identity(self) -> VirtualIdentity:
        return self._bus.identity

    def get(self, event_id: str) -> Optional[EventRecord]:
        return self._bus.get(event_id)

    def pending_count(self, event_type: Optional[str] = None) -> int:
        return self._bus.pending_count(event_type)

    def list_recent(
        self, event_type: Optional[str] = None, *, limit: int = 50
    ) -> list[Dict[str, Any]]:
        """只读查询扩展（鸭子面）：委托进程内总线的同名方法。"""
        return self._bus.list_recent(event_type, limit=limit)

    def to_persistence(self) -> Dict[str, Any]:
        return self._bus.to_persistence()

    # -- 日志 -------------------------------------------------------------

    def _append(self, op: str, rec: EventRecord) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            entry = {"op": op, **rec.to_dict()}
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            self._logged.add(rec.event_id)
        except OSError:
            # 日志失败不得让事件本身失败（内存语义仍生效）；可观测性由调用方决定
            pass
