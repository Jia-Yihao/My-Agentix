"""EventPort 生产装配与外部事件等待（Phase 20 规划 §8.2；Phase 23 扩后端）。

- ``EVENT_PORT_BACKEND=memory``（默认）→ :class:`InMemoryEventBus`（现状）；
- ``EVENT_PORT_BACKEND=jsonl`` → :class:`JsonlEventBus`（事件日志，重启恢复）；
- ``EVENT_PORT_BACKEND=redis_stream``（Phase 23）→ :class:`RedisStreamEventBus`
  （Redis Stream；客户端懒建——首个命令才触达网络，连接错误诚实传播不伪造）；
- 未知值 → 告警回退 memory（与 ``build_trajectory_sink`` 同一容错口径）；
- 初始化期 import/构造异常 → 告警回退 memory（仅初始化容错；运行期错误不吞）。

``wait_external_event``：把「等外部事件」挂上 ``WAITING_EXTERNAL_EVENT`` 生命周期
（状态转移 best-effort——状态机允许时不阻塞事件等待本身）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from loguru import logger

from agentix.agent.events.adapter import InMemoryEventBus
from agentix.agent.events.jsonl import DEFAULT_EVENT_LOG_PATH, JsonlEventBus
from agentix.config import settings

__all__ = [
    "EventPortLike",
    "build_event_port",
    "get_event_port",
    "reset_event_port",
    "wait_external_event",
    "DEFAULT_EVENT_LOG_PATH",
]

#: 结构类型（EventPort 协议的可调用面）——避免此处 import domain 造成多余耦合
EventPortLike = Any

_event_port: Optional[EventPortLike] = None


def build_event_port() -> EventPortLike:
    """按 ``EVENT_PORT_BACKEND`` 构造事件端口（未知值告警回退 memory）。"""
    kind = str(getattr(settings, "EVENT_PORT_BACKEND", "memory") or "memory").strip().lower()
    if kind == "jsonl":
        return JsonlEventBus(DEFAULT_EVENT_LOG_PATH)
    if kind == "redis_stream":
        try:
            from agentix.agent.events.redis_stream import RedisStreamEventBus

            return RedisStreamEventBus()  # 懒客户端：构造不连库
        except Exception as exc:  # noqa: BLE001 — 仅初始化容错（包缺失等）
            logger.warning("redis_stream event port init failed ({}), 回退内存 event port", exc)
            return InMemoryEventBus()
    if kind != "memory":
        logger.warning("未知的 EVENT_PORT_BACKEND={}，回退内存 event port", kind)
    return InMemoryEventBus()


def get_event_port() -> EventPortLike:
    """全局事件端口单例（惰性创建；测试用 ``reset_event_port`` 隔离）。"""
    global _event_port
    if _event_port is None:
        _event_port = build_event_port()
    return _event_port


def reset_event_port() -> None:
    """重置单例（测试隔离）。"""
    global _event_port
    _event_port = None


async def wait_external_event(
    state: Any,
    event_type: str,
    *,
    timeout_s: float = 0.0,
    port: Optional[EventPortLike] = None,
) -> Optional[Dict[str, Any]]:
    """等外部事件并挂 ``WAITING_EXTERNAL_EVENT`` 生命周期。

    - 进入等待前：``EXECUTING/RUNNING → WAITING_EXTERNAL_EVENT``（best-effort，
      非法转移自吞——事件等待不得被状态机拒绝拖死）；
    - 返回（命中/超时）：``WAITING_EXTERNAL_EVENT → EXECUTING``（同样 best-effort）；
    - 超时返回 ``None``，由调用方决定后续（FAILED / 重试 / 放弃）。
    """
    from agentix.runtime.lifecycle import LifecycleManager
    from agentix.runtime.state import AgentStatus

    active = port if port is not None else get_event_port()
    try:
        LifecycleManager.transition(state, AgentStatus.WAITING_EXTERNAL_EVENT)
    except Exception:  # noqa: BLE001 — best-effort 状态转移
        pass
    try:
        return await active.wait(event_type, timeout_s=timeout_s)
    finally:
        try:
            LifecycleManager.transition(state, AgentStatus.EXECUTING)
        except Exception:  # noqa: BLE001 — best-effort 状态转移
            pass
