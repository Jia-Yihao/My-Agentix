"""Hook 注册与触发（Phase 21）：同步 callable + 顺序触发 + 逐个隔离。

纪律（书 §4.4「hook 不得反向耦合」的落地）：

- **仅同步** ``Callable[[dict], None]``——异步回调不入此表（需异步者走
  ``review_blocked_hook`` 那类专用 awaitable 钩子，不混用）；
- ``emit()`` 第一行 gate ``settings.HOOKS_ENABLED``：默认关时一次回调
  都不触碰（零注册 ⇒ 零开销，正是 Strangler flag-off 语义）；
- 逐个 ``try/except`` + ``logger.warning``：**任何** hook 故障不改变主
  路径行为，也不吞掉后续 hook（注册顺序 == 触发顺序）。
"""

from __future__ import annotations

from threading import Lock
from typing import Callable, Dict, List, Tuple

from loguru import logger

from agentix.agent.hooks.events import EVENT_TYPES

__all__ = [
    "HookFn",
    "register",
    "clear_hooks",
    "hook_events",
    "emit",
]

#: hook 签名：payload 是普通 dict（进程内对象，不做序列化承诺）
HookFn = Callable[[dict], None]

_lock = Lock()
_hooks: Dict[str, List[HookFn]] = {}


def register(event: str, fn: HookFn) -> None:
    """按注册顺序追加一个 hook；未知事件名 ``ValueError``（防拼写静默）。"""
    if event not in EVENT_TYPES:
        raise ValueError(f"unknown hook event: {event!r} (valid: {EVENT_TYPES})")
    if not callable(fn):
        raise TypeError(f"hook must be callable, got {type(fn).__name__}")
    with _lock:
        _hooks.setdefault(event, []).append(fn)


def clear_hooks() -> None:
    """清空全部注册（测试隔离 / 卸载）。"""
    with _lock:
        _hooks.clear()


def hook_events() -> Tuple[str, ...]:
    """当前有注册者的事件名（注册顺序）。"""
    with _lock:
        return tuple(e for e in EVENT_TYPES if _hooks.get(e))


def emit(event: str, payload: Dict[str, object] | None = None) -> None:
    """触发一个事件；``HOOKS_ENABLED=false`` 时首行即返（零回调）。

    本函数**永不抛**——hook 故障只告警，主路径（工具执行/主循环）行为不变。
    """
    from agentix.config.settings import settings

    if not settings.HOOKS_ENABLED:
        return
    if event not in EVENT_TYPES:
        logger.warning("emit skipped: unknown hook event {!r}", event)
        return
    with _lock:
        callbacks = list(_hooks.get(event, ()))
    if not callbacks:
        return
    data: Dict[str, object] = dict(payload or {})
    data.setdefault("event", event)
    for fn in callbacks:
        try:
            fn(data)
        except Exception as exc:  # noqa: BLE001 — 逐个隔离：坏 hook 不拖垮好 hook
            logger.warning("hook {} failed (result unaffected): {}", event, exc)
