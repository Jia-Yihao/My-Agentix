"""R5a：Run作用域的 SSE 事件发射器（守卫 #5 载体）。

方案 v2 §1（总纲）：「SSE 只是 Run Event Stream 的投影」。本模块给
统一执行链的对外 SSE 帧打上统一戳记，使**每一帧**都可回溯到
Run/Event 源（守卫 #5：SSE event 必须来自统一 Run/Event stream）：

- ``seq``：本 Run 内单调递增的帧序号（从 1 起，每个帧必打）；
- ``run_id``：ControlPlane 登记成功的 Run 载体。

另含**性能真值戳记**：

- 首个内容帧（answer_chunk / content / message）→ 记录 TTFT 并打
  ``agent_ttft_seconds`` 直方图（每流一次）；T6 起按回退链导出的事实拆分归因——
  走过 Model Gateway 兜底的流记 ``agent_ttft_fallback_seconds``，主口径只记主路径；
- 终帧（finished / done / error，或检索域的 ``answer_chunk``+``finished=true``
  收尾 chunk）→ 增量注入 ``duration_ms``（流总时长）与已测得的
  ``ttft_ms``——**纯增量字段**，不改变既有词汇（p65 守卫）；
- ``LATENCY_BREAKDOWN_ENABLED=true`` 时注入 ``breakdown_ms``
  （ttft / retrieval 四阶段和 / stream），供前端耗时芯片与回放消费；
- ``LATENCY_TOTAL_BUDGET_MS>0`` 时评估超预算 → fail-open 留痕
  （warning + record_degraded("latency_budget")，绝不中断流）。

契约：

- 每条流**一个实例**（非线程安全；由 ``stream_events`` /
  ``_stream_answer_impl`` 的流作用域持有）；
- 登记失败（降级为不追踪）时 ``run_id`` 为空——帧仍带 ``seq``，
  诚实反映「有帧无 Run」的降级态，不静默伪造；
- ``attach`` 原地修改帧（帧为各 yield 点新造的 dict），返回同一对象，
  便于在 ``yield`` 单点串接。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, FrozenSet, Optional

logger = logging.getLogger(__name__)

# 首内容帧类型：检索域为 answer_chunk；编排域消息帧为 message。
_CONTENT_TYPES: FrozenSet[str] = frozenset({"answer_chunk", "content", "message"})
# 终帧类型：检索域 finished；编排域 done；两域 error。
_TERMINAL_TYPES: FrozenSet[str] = frozenset({"finished", "done", "error"})


class RunEventEmitter:
    """单 Run 作用域的帧戳记器（含 W0 性能戳记）。"""

    def __init__(self, run_id: Optional[str] = None) -> None:
        self.run_id = str(run_id) if run_id else ""
        self._seq = 0
        self._t0 = time.perf_counter()
        self._ttft_s: Optional[float] = None
        self._retrieval_ms: Optional[float] = None
        # T6：流开始即开本流的「兜底链事实」槽——判定由回退链自身写入
        # （model_gateway → record_gateway_fallback），发射器只读不判（约定 6 ④）。
        # 开槽失败 ⇒ 降级为「无事实」（归因回落主口径），留痕不打挂帧流（约定 2）。
        try:
            from agentix.observability.metrics import begin_gateway_fallback_fact

            begin_gateway_fallback_fact()
        except Exception as exc:  # noqa: BLE001 - 观测开槽失败留痕忽略，不影响主链
            logger.warning("ttft fallback attribution channel skipped: %s", exc)

    @property
    def seq(self) -> int:
        """已发出的帧数（= 下一帧将获得的序号 - 1）。"""
        return self._seq

    @property
    def ttft_ms(self) -> Optional[int]:
        """首内容帧时延（毫秒）；尚无内容帧则为 None。"""
        if self._ttft_s is None:
            return None
        return int(self._ttft_s * 1000)

    def attach(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        """给帧打上 ``seq`` / ``run_id``（原地修改并返回同一帧）。"""
        self._seq += 1
        frame["seq"] = self._seq
        if self.run_id:
            frame["run_id"] = self.run_id
        self._stamp_perf(frame)
        return frame

    # -- 性能戳记 ------------------------------------------------

    def _stamp_perf(self, frame: Dict[str, Any]) -> None:
        """TTFT / 终帧 duration 注入 / 延迟拆解与预算（全部 fail-open）。"""
        try:
            ftype = frame.get("type")
            if ftype in _CONTENT_TYPES and self._ttft_s is None:
                self._ttft_s = time.perf_counter() - self._t0
                try:
                    from agentix.observability.metrics import (
                        gateway_fallback_observed,
                        record_ttft,
                        record_ttft_fallback,
                    )

                    # T6：混采拆分——回退链已导出兜底事实的流，样本进
                    # agent_ttft_fallback_seconds；主直方图只记主口径。
                    # 「是否兜底」读的是链自身写入的事实，这里不再判定（约定 6 ④）。
                    if gateway_fallback_observed():
                        record_ttft_fallback(self._ttft_s)
                    else:
                        record_ttft(self._ttft_s)
                except Exception as exc:  # noqa: BLE001 - 指标失败不影响帧流（降级可观察）
                    logger.warning("ttft record skipped: %s", exc)
            elif ftype == "progress" and self._retrieval_ms is None:
                self._stash_retrieval_ms(frame)
            # 终帧：显式 finished/done/error 词汇，或检索域的
            # 「answer_chunk + finished=true」收尾 chunk。
            if ftype in _TERMINAL_TYPES or frame.get("finished") is True:
                self._stamp_terminal(frame)
        except Exception:  # noqa: BLE001 - 性能戳记绝不能打挂 SSE 帧流
            pass

    def _stash_retrieval_ms(self, frame: Dict[str, Any]) -> None:
        """从 progress 帧的四阶段报告提取检索总耗时（detail.stages[].ms）。"""
        detail = frame.get("detail")
        if not isinstance(detail, dict):
            return
        stages = detail.get("stages")
        if not isinstance(stages, list) or not stages:
            return
        try:
            total = sum(float(s.get("ms", 0)) for s in stages if isinstance(s, dict))
        except (TypeError, ValueError):
            return
        if total > 0:
            self._retrieval_ms = total

    def _stamp_terminal(self, frame: Dict[str, Any]) -> None:
        """终帧注入 duration_ms / ttft_ms /（可选）breakdown_ms 与预算评估。"""
        duration_ms = int((time.perf_counter() - self._t0) * 1000)
        frame.setdefault("duration_ms", duration_ms)
        if self._ttft_s is not None:
            frame.setdefault("ttft_ms", int(self._ttft_s * 1000))

        from agentix.config import settings

        if getattr(settings, "LATENCY_BREAKDOWN_ENABLED", False):
            breakdown: Dict[str, int] = {"stream_ms": duration_ms}
            if self._ttft_s is not None:
                breakdown["ttft_ms"] = int(self._ttft_s * 1000)
            if self._retrieval_ms is not None:
                breakdown["retrieval_ms"] = int(self._retrieval_ms)
            frame.setdefault("breakdown_ms", breakdown)

        if getattr(settings, "LATENCY_TOTAL_BUDGET_MS", 0) > 0:
            from agentix.performance.budget import enforce as enforce_budget

            enforce_budget(float(duration_ms), source="run_stream")
