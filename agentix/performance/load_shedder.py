"""七级降级阶梯 + 负载甩脱（L0 全量 … L6 礼貌拒绝）。

输入（flag 关时全部不读，零采样零开销）：

1. **event-loop lag** —— W0 后台采样任务的最近一次值（``record_event_loop_lag``
   同步落模块变量，``get_last_event_loop_lag()`` 读取）；
2. **准入队列深度** —— ``performance.admission`` 的等位计数/容量；
3. **下游熔断态** —— ``reliability.circuit_breaker`` registry 中非 CLOSED 的
   熔断器数量（延迟突增会先打到熔断器）。

阶梯语义（:func:`classify_load` 纯函数，取各输入贡献的最大级别）：

- L1 lag≥0.10s / 队列非空 —— 起甩 evolution 后台；
- L2 lag≥0.25s / 队列≥半满 / 下游熔断 —— + eval；
- L3 lag≥0.50s / 队列≥3/4 —— + 记忆固化；
- L4 lag≥1.0s / 队列满 —— + 会话摘要；
- L5 lag≥2.0s —— 全部后台（含文件导入）；
- L6 lag≥5.0s 或（队列满且 lag≥1.0s）—— 礼貌拒绝新请求
  （与 ``admission_wait`` 合流抛 ``AdmissionDenied`` → 429）。

降级经既有 ``record_degraded`` 留痕（级别上行时按新级别记一次）。
"""

from __future__ import annotations

from enum import IntEnum
from typing import Dict

from loguru import logger

from agentix.config import settings

__all__ = [
    "ShedLevel",
    "classify_load",
    "LoadShedder",
    "get_load_shedder",
    "reset_load_shedder",
]


class ShedLevel(IntEnum):
    """七级降级阶梯（L0 = 全量无降级）。"""

    L0 = 0
    L1 = 1
    L2 = 2
    L3 = 3
    L4 = 4
    L5 = 5
    L6 = 6


# -- 分级阈值（模块常量；规划未列 settings 化需求，避免为阈值扩五处同步）---
_LAG_L1 = 0.10
_LAG_L2 = 0.25
_LAG_L3 = 0.50
_LAG_L4 = 1.0
_LAG_L5 = 2.0
_LAG_L6 = 5.0
_LAG_L6_WITH_FULL_QUEUE = 1.0  # 队列满且 lag≥1s → L6（礼貌拒绝）

#: 后台任务类型 → 类别（``_process_message`` 甩负荷归类）。
TASK_TYPE_CATEGORY: Dict[str, str] = {
    "evolution_sleep": "evolution",
    "file_import": "import",
    "conversation_summary": "summary",
    "memory_consolidation": "memory",
}

#: 类别 → 首个甩掉该类的级别（从先到后；L5 兜底全后台）。
_CATEGORY_LEVEL: Dict[str, ShedLevel] = {
    "evolution": ShedLevel.L1,
    "eval": ShedLevel.L2,
    "memory": ShedLevel.L3,
    "summary": ShedLevel.L4,
    "import": ShedLevel.L5,
}


class LoadSignals:
    """一拍负载输入（文档化的取数口径；classify_load 直接收同名关键字）。"""

    __slots__ = ("lag_s", "queue_depth", "queue_capacity", "impaired_downstreams")

    def __init__(
        self,
        *,
        lag_s: float = 0.0,
        queue_depth: int = 0,
        queue_capacity: int = 0,
        impaired_downstreams: int = 0,
    ) -> None:
        self.lag_s = max(0.0, float(lag_s))
        self.queue_depth = max(0, int(queue_depth))
        self.queue_capacity = max(0, int(queue_capacity))
        self.impaired_downstreams = max(0, int(impaired_downstreams))


def classify_load(
    *,
    lag_s: float = 0.0,
    queue_depth: int = 0,
    queue_capacity: int = 0,
    impaired_downstreams: int = 0,
) -> ShedLevel:
    """纯函数：负载输入 → 阶梯级别（各输入贡献取最大，单调不回退由调用方管）。"""
    level = ShedLevel.L0

    if lag_s >= _LAG_L6:
        level = max(level, ShedLevel.L6)
    elif lag_s >= _LAG_L5:
        level = max(level, ShedLevel.L5)
    elif lag_s >= _LAG_L4:
        level = max(level, ShedLevel.L4)
    elif lag_s >= _LAG_L3:
        level = max(level, ShedLevel.L3)
    elif lag_s >= _LAG_L2:
        level = max(level, ShedLevel.L2)
    elif lag_s >= _LAG_L1:
        level = max(level, ShedLevel.L1)

    if queue_capacity > 0 and queue_depth > 0:
        ratio = queue_depth / queue_capacity
        if ratio >= 1.0:
            level = max(
                level,
                ShedLevel.L6 if lag_s >= _LAG_L6_WITH_FULL_QUEUE else ShedLevel.L4,
            )
        elif ratio >= 0.75:
            level = max(level, ShedLevel.L3)
        elif ratio >= 0.5:
            level = max(level, ShedLevel.L2)
        else:
            level = max(level, ShedLevel.L1)

    if impaired_downstreams >= 2:
        level = max(level, ShedLevel.L4)
    elif impaired_downstreams == 1:
        level = max(level, ShedLevel.L2)

    return level


def _last_lag_s() -> float:
    try:
        from agentix.observability.metrics import get_last_event_loop_lag

        return get_last_event_loop_lag()
    except Exception:  # noqa: BLE001 - lag 读不到按 0（不因观测失败升阶）
        return 0.0


def _impaired_downstreams() -> int:
    try:
        from agentix.reliability.circuit_breaker import registry as breaker_registry

        impaired = 0
        for stats in breaker_registry.all_stats():
            state = str(getattr(stats, "state", "")).lower()
            if "closed" not in state:
                impaired += 1
        return impaired
    except Exception:  # noqa: BLE001
        return 0


class LoadShedder:
    """进程级负载甩脱器（每次读 settings + 现场取输入）。"""

    def __init__(self) -> None:
        self._reported_level = ShedLevel.L0

    def current_level(self) -> ShedLevel:
        """当前阶梯（flag 关恒 L0；上行迁移 record_degraded 留痕一次）。"""
        if not bool(getattr(settings, "LOAD_SHEDDING_ENABLED", False)):
            return ShedLevel.L0

        from agentix.performance.admission import queue_capacity, queue_depth

        level = classify_load(
            lag_s=_last_lag_s(),
            queue_depth=queue_depth(),
            queue_capacity=queue_capacity(),
            impaired_downstreams=_impaired_downstreams(),
        )
        if level > self._reported_level:
            self._reported_level = level
            self._record(level)
        return level

    def _record(self, level: ShedLevel) -> None:
        logger.warning("load shed level raised to {}", level.name)
        try:
            from agentix.observability.metrics import record_degraded

            record_degraded(f"load_shed_{level.name.lower()}")
        except Exception:  # noqa: BLE001 - 观测失败不影响甩脱判定
            logger.debug("load shed degraded counter skipped", exc_info=True)

    def should_shed(self, task_type: str) -> bool:
        """后台任务是否该被甩掉（未知类型保守不甩；L0/L6 恒 False——
        L6 的礼貌拒绝在 admission 侧）。"""
        if not bool(getattr(settings, "LOAD_SHEDDING_ENABLED", False)):
            return False
        level = self.current_level()
        if level <= ShedLevel.L0 or level >= ShedLevel.L6:
            return False
        category = TASK_TYPE_CATEGORY.get(str(task_type))
        if category is None:
            return False
        required = _CATEGORY_LEVEL.get(category)
        if required is None:  # pragma: no cover - 类别表全覆盖
            return False
        return level >= required


_shedder: LoadShedder | None = None


def get_load_shedder() -> LoadShedder:
    """进程级单例。"""
    global _shedder
    if _shedder is None:
        _shedder = LoadShedder()
    return _shedder


def reset_load_shedder() -> None:
    """测试隔离：丢弃单例（上行留痕状态归零）。"""
    global _shedder
    _shedder = None
