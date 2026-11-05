"""性能真值：端到端延迟拆解与预算。

设计要点：

- ``LatencyBreakdown``：一次请求/流的分阶段耗时账本（毫秒）。
- ``phase(name)``：同步上下文管理器，测量代码块耗时并记入**当前**账本；
  在 async 函数内包住 ``await`` 块即可（进入/退出是同步的，await 发生在中间）。
- ``capture()``：在作用域内创建并绑定一个新账本（contextvar），返回它。
- 账本未绑定时 ``phase`` 为零开销 no-op —— 埋点缺失绝不影响主链路。

生产接线（W0）：RunEventEmitter 终帧注入 ``breakdown_ms``（ttft/retrieval/stream，
见 agentix/application/services/run_event_stream.py）；``phase()`` 作为通用
计时工具供后续波次（W3 准入排队等待、W4 批写延迟等）消费。
"""

from __future__ import annotations

import contextlib
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional


@dataclass
class PhaseSample:
    """单阶段耗时样本。"""

    name: str
    duration_ms: float


@dataclass
class LatencyBreakdown:
    """一次作用域内的分阶段延迟账本（保序，同名阶段累加）。"""

    samples: List[PhaseSample] = field(default_factory=list)

    def add(self, name: str, duration_ms: float) -> None:
        self.samples.append(PhaseSample(name=name, duration_ms=max(0.0, duration_ms)))

    @property
    def total_ms(self) -> float:
        return sum(s.duration_ms for s in self.samples)

    def to_dict(self) -> Dict[str, int]:
        """聚合成 ``{阶段名: 毫秒}``（同名求和，int 化便于 JSON 序列化）。"""
        agg: Dict[str, float] = {}
        for s in self.samples:
            agg[s.name] = agg.get(s.name, 0.0) + s.duration_ms
        return {k: int(v) for k, v in agg.items()}


_breakdown_var: ContextVar[Optional[LatencyBreakdown]] = ContextVar(
    "agentix_latency_breakdown", default=None
)


def current() -> Optional[LatencyBreakdown]:
    """当前作用域的延迟账本；未绑定返回 None（no-op 语义）。"""
    return _breakdown_var.get()


def reset() -> None:
    """解绑当前账本（测试/流结束清理）。"""
    _breakdown_var.set(None)


@contextlib.contextmanager
def capture() -> Iterator[LatencyBreakdown]:
    """绑定一个新账本到当前作用域；退出时保留账本对象供调用方读取。"""
    bd = LatencyBreakdown()
    token = _breakdown_var.set(bd)
    try:
        yield bd
    finally:
        _breakdown_var.reset(token)


@contextlib.contextmanager
def phase(name: str) -> Iterator[None]:
    """测量代码块耗时并记入当前账本；无账本绑定时仅计时不记账。"""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        bd = _breakdown_var.get()
        if bd is not None:
            bd.add(name, (time.perf_counter() - t0) * 1000.0)
