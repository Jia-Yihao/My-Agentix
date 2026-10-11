"""进程内 asyncio 消息总线（Phase 25，书 §10.3 Message Bus 本义）。

《My Agentix_架构优化方案》Ch10：Peer 间「点对点直连」缺少统一的通信面，
取消与观测要逐点打补丁。本模块提供**最小原语**：

* ``discover()``   —— 已注册的 agent 列表；
* ``send()``       —— 同步投递（``put_nowait``，不阻塞调用方）；
* ``recv()``       —— 异步消费（可超时）；
* ``cancel()``     —— **peer 粒度取消**（六模式之五「取消 fan-out」的落点）；
* ``status()``     —— 队列深度 / 投递计数观测。

设计约束（Strangler + 纪律）：

* **纯 asyncio + dict**：零中间件、零新依赖；**不复用 Redis Stream 作 bus**
  （职责分离：事件总线 = 系统事件；agent 总线 = Peer 通信，二者语义不同）；
* **有界**：每订阅者队列 ``maxsize``，满则丢弃并计数——防 livelock 场景下
  内存无界膨胀（宁可丢消息计数可观测，也不把进程拖死）；
* **不抛网络异常**：进程内投递失败只体现为返回 ``False`` + 计数。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agentix.orchestration.strategies.multi_agent.peer import PeerMessage

__all__ = [
    "BusStats",
    "InProcessBus",
]


@dataclass
class BusStats:
    """总线累计计数（可整体序列化进 meta 供观测）。"""

    sent: int = 0
    #: 未注册接收方 / 队列已满 / 接收方已取消 的丢弃数
    dropped: int = 0
    #: ``cancel()`` 时被清空的在途消息数
    cancelled_in_flight: int = 0
    cancel_calls: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sent": self.sent,
            "dropped": self.dropped,
            "cancelled_in_flight": self.cancelled_in_flight,
            "cancel_calls": self.cancel_calls,
        }


class InProcessBus:
    """进程内消息总线：订阅、投递、消费、peer 粒度取消、状态观测。"""

    def __init__(self, *, maxsize: int = 64) -> None:
        self._maxsize = max(1, int(maxsize))
        self._queues: Dict[str, asyncio.Queue] = {}
        self._cancelled: set = set()
        self._stats = BusStats()

    # -- 注册 / 发现 ---------------------------------------------------------

    def subscribe(self, agent_id: str) -> asyncio.Queue:
        """注册订阅者（幂等），返回其收件队列。"""
        queue = self._queues.get(agent_id)
        if queue is None:
            queue = asyncio.Queue(maxsize=self._maxsize)
            self._queues[agent_id] = queue
        return queue

    def unsubscribe(self, agent_id: str) -> bool:
        """注销订阅者（在途消息随之丢弃）。"""
        return self._queues.pop(agent_id, None) is not None

    def discover(self) -> List[str]:
        """已注册 agent 列表（排序，确定性）。"""
        return sorted(self._queues)

    # -- 投递 / 消费 ---------------------------------------------------------

    def send(self, message: PeerMessage) -> bool:
        """同步投递到接收方队列；不阻塞、不抛异常。

        返回 ``False`` 的情形（均计数 ``dropped``）：接收方未注册、
        接收方已 ``cancel``、队列已满。
        """
        if message.receiver in self._cancelled:
            self._stats.dropped += 1
            return False
        queue = self._queues.get(message.receiver)
        if queue is None:
            self._stats.dropped += 1
            return False
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            self._stats.dropped += 1
            return False
        self._stats.sent += 1
        return True

    async def recv(self, agent_id: str, *, timeout: Optional[float] = None) -> PeerMessage:
        """消费一条给 ``agent_id`` 的消息；未注册抛 ``KeyError``。

        ``timeout`` 到期抛 :class:`asyncio.TimeoutError`（消费方自行决定策略）。
        """
        queue = self._queues.get(agent_id)
        if queue is None:
            raise KeyError(f"agent not subscribed: {agent_id!r}")
        if timeout is None:
            return await queue.get()
        return await asyncio.wait_for(queue.get(), timeout)

    # -- peer 粒度取消 -------------------------------------------------------

    def cancel(self, agent_id: str) -> int:
        """取消一个 peer：后续发往它的消息直接拒收，并清空其在途队列。

        :return: 被清空的在途消息数。
        """
        self._stats.cancel_calls += 1
        self._cancelled.add(agent_id)
        queue = self._queues.get(agent_id)
        drained = 0
        if queue is not None:
            while not queue.empty():
                try:
                    queue.get_nowait()
                    drained += 1
                except asyncio.QueueEmpty:  # pragma: no cover - 竞态防御
                    break
        self._stats.cancelled_in_flight += drained
        return drained

    def resume(self, agent_id: str) -> bool:
        """解除 peer 取消标记（测试与重试场景）；返回之前是否处于取消态。"""
        if agent_id in self._cancelled:
            self._cancelled.discard(agent_id)
            return True
        return False

    def is_cancelled(self, agent_id: str) -> bool:
        return agent_id in self._cancelled

    # -- 观测 ---------------------------------------------------------------

    def status(self, agent_id: Optional[str] = None) -> Dict[str, Any]:
        """总线状态（``agent_id`` 给定时返回该 peer 的队列状态）。"""
        if agent_id is not None:
            queue = self._queues.get(agent_id)
            return {
                "agent_id": agent_id,
                "subscribed": queue is not None,
                "queue_size": queue.qsize() if queue is not None else 0,
                "cancelled": agent_id in self._cancelled,
            }
        return {
            "subscribers": len(self._queues),
            "maxsize": self._maxsize,
            "cancelled_peers": sorted(self._cancelled),
            **self._stats.to_dict(),
        }

    def stats(self) -> Dict[str, Any]:
        return self._stats.to_dict()
