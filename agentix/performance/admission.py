"""运行准入三态（放行 / 排队 / 拒绝 429）。

挂 **Control Plane 入口**——在 ``ControlPlane.spawn`` 之前 ``await admission_wait()``：

- ``ADMISSION_CONTROL_ENABLED=false``（默认）：首行返回，与接线前逐位等价；
- 开启后并发账本复用平面 ``active_count()``（登记即 +1、终态自动释放，
  本模块**不持有**释放句柄，杜绝「忘了释放」类泄漏）；
- 三态判据 = 全局并发（``ADMISSION_MAX_CONCURRENT_RUNS``，0 回落
  ``CONTROL_PLANE_MAX_CONCURRENCY``，两者皆 0 = 不限）+ 排队深度
  （``ADMISSION_MAX_QUEUE``，0 = 超限即拒）；
- 排队等位有界（模块常量 30s），超时仍拒；
- ``LOAD_SHEDDING_ENABLED`` 且负载阶梯到 L6 时**先于**并发判据礼貌拒绝。

拒绝统一抛 ``agentix.runtime.control_plane.AdmissionDenied``（fail-closed），
HTTP 层由全局异常处理器映射 429。依赖方向：惰性 import（performance →
runtime / observability 不在模块加载期发生）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from loguru import logger

from agentix.config import settings

__all__ = [
    "AdmissionDecision",
    "AdmissionController",
    "admission_wait",
    "queue_depth",
    "queue_capacity",
    "get_admission_controller",
    "reset_admission_controller",
]

#: 排队等位上限（秒）——超时即拒；刻意为模块常量（非 settings 键），
#: 避免为一个运维兜底值扩一轮五处同步。
ENQUEUE_TIMEOUT_S = 30.0
#: 等位轮询间隔（秒）。
_POLL_INTERVAL_S = 0.05


@dataclass(frozen=True)
class AdmissionDecision:
    """三态判定结果（可观测口径）。"""

    state: str  # "admitted" | "queued" | "rejected"
    reason: str = ""
    waited_ms: float = 0.0


def _effective_max_concurrent() -> int:
    """准入并发上限：显式配置 >0 用之，否则回落 CONTROL_PLANE 上限。"""
    explicit = int(getattr(settings, "ADMISSION_MAX_CONCURRENT_RUNS", 0) or 0)
    if explicit > 0:
        return explicit
    return int(getattr(settings, "CONTROL_PLANE_MAX_CONCURRENCY", 0) or 0)


def _active_runs() -> int:
    try:
        from agentix.runtime.control_plane import get_control_plane

        return int(get_control_plane().active_count())
    except Exception as exc:  # noqa: BLE001 - 平面故障时按 0 计（不因观测失败卡死准入）
        logger.debug("admission active_count unavailable: {}", exc)
        return 0


class AdmissionController:
    """进程内准入控制器（无释放句柄；账本 = 平面 active_count）。"""

    def __init__(self) -> None:
        self._waiters = 0

    @property
    def waiters(self) -> int:
        return self._waiters

    async def admit(self) -> AdmissionDecision:
        """三态判定：放行 / 排队等位（有界）/ 拒绝。"""
        max_concurrent = _effective_max_concurrent()
        max_queue = int(getattr(settings, "ADMISSION_MAX_QUEUE", 0) or 0)
        active = _active_runs()

        # 不限（配置两处皆 0）：恒放行（队列不参与）。
        if max_concurrent <= 0:
            return AdmissionDecision(state="admitted")
        if active < max_concurrent:
            return AdmissionDecision(state="admitted")
        if max_queue <= 0:
            return AdmissionDecision(
                state="rejected",
                reason=f"active runs {active} >= max {max_concurrent} (queue disabled)",
            )
        if self._waiters >= max_queue:
            return AdmissionDecision(
                state="rejected",
                reason=f"admission queue full ({self._waiters}/{max_queue})",
            )

        # 入队等位：轮询平面 active_count（终态释放即让位）。
        self._waiters += 1
        t0 = time.monotonic()
        try:
            deadline = t0 + ENQUEUE_TIMEOUT_S
            while _active_runs() >= max_concurrent:
                if time.monotonic() >= deadline:
                    return AdmissionDecision(
                        state="rejected",
                        reason=(
                            f"admission queue timeout after {ENQUEUE_TIMEOUT_S:.0f}s "
                            f"(active {active} >= max {max_concurrent})"
                        ),
                        waited_ms=(time.monotonic() - t0) * 1000.0,
                    )
                await asyncio.sleep(_POLL_INTERVAL_S)
            return AdmissionDecision(
                state="queued",
                waited_ms=(time.monotonic() - t0) * 1000.0,
            )
        finally:
            self._waiters -= 1


_controller: AdmissionController | None = None


def get_admission_controller() -> AdmissionController:
    """进程级单例（每次读 settings，测试 monkeypatch 后无需重建）。"""
    global _controller
    if _controller is None:
        _controller = AdmissionController()
    return _controller


def reset_admission_controller() -> None:
    """测试隔离：丢弃单例（等位计数一并归零）。"""
    global _controller
    _controller = None


def queue_depth() -> int:
    """当前等位深度（负载阶梯输入；flag 关时恒 0）。"""
    if not bool(getattr(settings, "ADMISSION_CONTROL_ENABLED", False)):
        return 0
    return get_admission_controller().waiters


def queue_capacity() -> int:
    """排队容量（负载阶梯输入；0 = 未启用排队）。"""
    if not bool(getattr(settings, "ADMISSION_CONTROL_ENABLED", False)):
        return 0
    return int(getattr(settings, "ADMISSION_MAX_QUEUE", 0) or 0)


def _record(state: str) -> None:
    try:
        from agentix.observability.metrics import record_admission_decision

        record_admission_decision(state)
    except Exception:  # noqa: BLE001 - 观测失败不改准入结果
        logger.debug("admission metric skipped", exc_info=True)


async def admission_wait(*, kind: str = "agent") -> None:
    """准入闸（spawn 前唯一 await 点）。

    放行/排队后正常返回；拒绝抛 :class:`AdmissionDenied`（fail-closed，
    调用方沿既有 ``except AdmissionDenied`` 契约处理，HTTP 层映射 429）。
    """
    if not bool(getattr(settings, "ADMISSION_CONTROL_ENABLED", False)):
        return

    # 负载阶梯 L6（礼貌拒绝）先于并发判据。
    if bool(getattr(settings, "LOAD_SHEDDING_ENABLED", False)):
        try:
            from agentix.performance.load_shedder import get_load_shedder

            level = get_load_shedder().current_level()
            if level.value >= 6:
                _record("rejected")
                from agentix.runtime.control_plane import AdmissionDenied

                raise AdmissionDenied(f"admission denied: load shed L6 ({kind})")
        except ImportError:  # pragma: no cover - load_shedder 恒存在
            pass

    decision = await get_admission_controller().admit()
    _record(decision.state)
    if decision.state == "rejected":
        from agentix.runtime.control_plane import AdmissionDenied

        logger.warning("admission rejected ({}): {}", kind, decision.reason)
        raise AdmissionDenied(f"admission denied: {decision.reason}")
    if decision.state == "queued":
        logger.info(
            "admission queued ({}): waited {:.1f}ms", kind, decision.waited_ms
        )
