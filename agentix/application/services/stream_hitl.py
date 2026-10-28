"""T23 流内审批（``STREAM_HITL_ENABLED``，默认关）：审批帧 + 等待期心跳 + 裁决后继续。

**为什么不能照抄非流式 ask 的做法**：那边（F1e·A-P1-4）用
``langgraph.types.interrupt`` 把图挂起（checkpointer 快照 + ``Command(resume=)``
续跑）。流只有**一条** SSE 响应可写——图一挂起，这条响应就再没有后续帧，客户端
只能干等到 ``STREAM_IDLE_TIMEOUT`` 被判成超时，用户甚至看不到"需要你批一下"。
流内因此**不挂图**：把"等一个裁决"放进节点内（``run_task_execution`` 的
``review_interrupt`` 回调），本模块负责把这段等待变成"可发帧"的等待——先发审批帧，
再按 ``STREAM_HITL_HEARTBEAT_S`` 发心跳，裁决落地后把决定交回节点
（``approve`` ⇒ 落批准后重试一次；其余 fail-closed，与非流式同一套语义）。

**判定不在这里**（纪律 6 ④：同一个判定不许两个源）：

- "要不要审批"由执行点的风险闸与闸门 ``preflight`` 判：只有
  ``reason="review_required"`` 的失败才会走到本模块的 ``request()``；
- "裁决是什么"的**唯一真源**是 T18 的库行 ``hitl_pending_approvals``：
  ``bridge.decide``（``/ops/hitl/approvals/{id}``）与 T19 到期自动拒绝都闭合**同一行**；
  本地闸门簿记（``get_review_gate().get(request_id).decision``）是同一事实的
  **进程内快路径**。因此本模块的判定顺序是「**本地闸门优先、未决时回捞库**」——
  两处读的是**同一条库记录**，不是两份裁决（纪律 6 ④）。回捞是给 ``--workers 2``
  （``docker-compose.yml`` 的默认形态）兜底：裁决落在**另一个 worker** 时，本进程
  只能从库里看见终态；只读本地闸门会让等流永久停在 PENDING，最后由 ``AGENT_TIMEOUT``
  以"处理请求超时"收口——那不是治理终局。本模块只读，不另建裁决缓存。

**时限口径**（四层，完整表见 ``docs/architecture/运行时护栏.md`` §16）：
流内审批等待**不算** ``STREAM_IDLE_TIMEOUT`` 的 idle（等待期由心跳帧占位），
但**仍受** ``AGENT_TIMEOUT``（整流墙钟）约束——墙钟超限照旧 error + done 收口，
节点随之被取消；``HITL_APPROVAL_DEADLINE_S``（T19）若开启，先到者生效。
节点内另有一道兜底上限 ``wait_cap_s``：到期返回 ``reject``，节点据此走
``task_execution`` 的拒绝分支 ``gate.reject(by="in_loop_resume")``——**那条 pending
因此被闭合**，归因是"流内等待上限触发的拒绝"；T19 的到期拒绝是**另一条来源**
（``decided_by=system:deadline`` + ``decision_reason=auto_reject_timeout``），
两者在归因上可区分。库行**不由本模块写**（仍是 ``pending``，后续由 T19 扫描或运维收口，
两条分支见 §16）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from loguru import logger

from agentix.config import settings

__all__ = [
    "APPROVAL_FRAME_TYPE",
    "HEARTBEAT_FRAME_TYPE",
    "DECISION_POLL_S",
    "StreamHitlChannel",
    "build_stream_hitl",
]

#: 审批帧类型：流内出现需人工裁决的工具时下发，``metadata.status`` 区分
#: ``pending``（等裁决）与 ``decided``（裁决已落地）。**前端未交付**：``web/``
#: 零改动（``web/src`` 下 ``approval`` / ``heartbeat`` 零命中），
#: ``web/src/modules/chat/api/chatStream.ts:22`` 的 ``ChatStreamChunk.type`` 仍是
#: 六值联合、``dispatchChunk`` 对这两型走 ``default`` 分支直接丢弃——本卡只产出
#: 后端帧，浏览器端渲染另立排期。
APPROVAL_FRAME_TYPE = "approval"

#: 心跳帧类型：审批等待期按 ``STREAM_HITL_HEARTBEAT_S`` 周期下发，占住 idle 看门狗
#: 与中间层连接超时。它**不是**业务事件，消费方可以忽略（丢弃也不影响终态）。
HEARTBEAT_FRAME_TYPE = "heartbeat"

#: 裁决轮询间隔（秒）。**模块常量而非配置项**：闸门簿记是内存字典查询，改它不影响
#: 正确性，但**不是没有运维语义**——它同时是裁决的**可见延迟上界**（与
#: ``stream_gate.COALESCE_WINDOW_S`` 同一取舍），以及 persist 开启时等待流对
#: ``hitl_pending_approvals`` 的**读频率**：未决时每拍回捞一次库行。不为此新增配置键（纪律 4）。
DECISION_POLL_S = 0.25

#: 库行终态 → 流内裁决（两值：``review_interrupt`` 只认 ``approve`` / ``reject``）。
#: 键集合 = T18 的 ``DECIDED_STATUSES``（库状态词表唯一真源）；``edited`` / ``resumed``
#: 在桥里与 ``approved`` 走同一条 ``gate.approve`` 路径，故与 ``approved`` 同值。
#: 未列出的取值（``pending`` / 未知）⇒ 不回捞成裁决，继续等。
_STORE_STATUS_DECISION: Dict[str, str] = {
    "approved": "approve",
    "edited": "approve",
    "resumed": "approve",
    "rejected": "reject",
}

#: ``anext`` 抛 ``StopAsyncIteration`` 时的占位（缓冲槽不能存异常对象本身）
_STOP = object()
#: 缓冲槽空标记
_EMPTY = object()
#: ``_wait_event_or_request`` 的"审批请求到达"标记
_OPENED = object()
#: ``_store`` 未解析标记（``None`` 本身是"本卡没开落库"这个合法结果，不能兼作哨兵）
_STORE_UNSET = object()

#: 装配缺口只告警一次（每进程）：装配在每个请求里发生，逐请求刷同一句 warning
#: 会把真正需要看的日志淹掉。
_WARNED: set = set()


def _warn_once(key: str, message: str, *args: Any) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    logger.warning(message, *args)


def build_stream_hitl(config: Dict[str, Any]) -> Optional["StreamHitlChannel"]:
    """按 flag 装配流内审批通道；``None`` = 不介入（与基线逐位等价，纪律 1）。

    flag 链（全开才装配，缺一即 ``None`` 且**不碰 config**）：

    - ``STREAM_HITL_ENABLED``：本卡总闸（默认关）；
    - ``HITL_IN_LOOP_ENABLED``：进环 HITL 语义的开槽（与非流式 ask 同一族口径）；
    - ``HUMAN_REVIEW_ENABLED``：父闸——它关着，闸门根本不拦工具，通道装配了也永远
      不会被调用。

    前两者开而父闸关属**死配置**：显式告警一次（不静默假装在管）。
    """
    if not bool(getattr(settings, "STREAM_HITL_ENABLED", False)):
        return None
    if not (
        bool(getattr(settings, "HITL_IN_LOOP_ENABLED", False))
        and bool(getattr(settings, "HUMAN_REVIEW_ENABLED", False))
    ):
        _warn_once(
            "stream_hitl_parents",
            "STREAM_HITL_ENABLED=true but HITL_IN_LOOP_ENABLED/HUMAN_REVIEW_ENABLED "
            "is off: stream-side approvals stay disabled (no approval frame will be emitted)",
        )
        return None
    channel = StreamHitlChannel(
        heartbeat_s=float(settings.STREAM_HITL_HEARTBEAT_S),
        poll_s=DECISION_POLL_S,
        idle_timeout=float(settings.STREAM_IDLE_TIMEOUT),
        # 兜底上限：量的是"这次等待本身"，管的是「流侧循环没在泵 `next_item`
        # （消费端已断开、或只跑节点的单测）」那种形态——那时 `chat_service` 的墙钟
        # 比较根本不执行，光靠它收不住。到期 fail-closed 返回 ``reject``（不留无限
        # 挂起的节点），节点据此 ``gate.reject(by="in_loop_resume")`` **闭合**该 pending，
        # 归因是"流内等待上限触发的拒绝"（与 T19 的 ``system:deadline`` 不同源，见 §16）。
        wait_cap_s=float(settings.AGENT_TIMEOUT),
    )
    configurable = config.setdefault("configurable", {})
    # 进环意图键与非流式 ask **同一个**（`lg_builder` 注入壳据此塞回调）：
    # 不另写一份"流要不要进环"的判据。
    configurable["hitl_in_loop"] = 1
    # 流侧通道本体。注入壳在本键存在时优先用它（节点内等待），不走 langgraph
    # interrupt——流没有第二条响应可用来续跑挂起的图。
    configurable["stream_hitl"] = channel
    return channel


class StreamHitlChannel:
    """流内审批通道：节点侧 :meth:`request` ↔ 流侧 :meth:`next_item`，同一事件循环内配对。

    两侧时序（正常一次挂起）::

        节点 execute_task → 工具 review_required → task_execution 攒 pending
          → await channel.request(payload)      # 节点挂在这里，不挂图
        流侧 next_item → 先发 approval(pending) → 每 heartbeat 秒发 heartbeat
          → 每 poll 秒读闸门裁决 → 裁决落地 ⇒ 发 approval(decided) 并放行节点
        节点 approve ⇒ gate.approve（或已是 APPROVED）+ 重试一次；其余 fail-closed
    """

    def __init__(
        self,
        *,
        heartbeat_s: float,
        poll_s: float,
        idle_timeout: float,
        wait_cap_s: Optional[float] = None,
    ) -> None:
        self._heartbeat_s = max(float(heartbeat_s), 0.001)
        self._poll_s = max(float(poll_s), 0.001)
        self._idle_timeout = float(idle_timeout)
        #: 单次等待的兜底上限（秒）；``None`` = 不设。到期 fail-closed 返回 ``reject``，
        #: 由节点侧 ``gate.reject(by="in_loop_resume")`` 闭合该 pending（本模块不越权
        #: 替治理层写裁决，见模块 docstring 与 ``运行时护栏.md`` §16）。
        self._wait_cap_s = float(wait_cap_s) if wait_cap_s else None
        #: 当前挂起：(payload, decision future)；同一时刻至多一个（同进程单飞契约）
        self._pending: Optional[Tuple[Dict[str, Any], "asyncio.Future[str]"]] = None
        #: 本轮审批帧是否已下发（首次 tick 只发审批帧，不睡也不轮询）
        self._announced = False
        #: 上次心跳时刻 / 本次等待起点（monotonic，仅用于 dt 与 waited_s）
        self._last_beat = 0.0
        self._wait_started = 0.0
        #: 审批请求到达信号（流侧在等事件时也要能被它唤醒）
        self._opened = asyncio.Event()
        #: T18 库面（回捞用）的惰性缓存；``_STORE_UNSET`` = 还没解析过。
        #: 解析判据的唯一源是 ``hitl_bridge.resolve_pending_store``（本模块不另读 flag）。
        self._store: Any = _STORE_UNSET
        #: 回捞失败只告警一次（每通道）：待审期间每拍都可能失败，逐拍刷同一句
        #: warning 会把真正需要看的日志淹掉；换一条请求会再告警一次。
        self._store_warned = False
        #: 在飞的 ``__anext__`` 任务（跨 ``next_item`` 调用保活）
        self._anext_task: Optional["asyncio.Future[Any]"] = None
        #: 事件缓冲：审批请求与事件同时到达时，事件先存这里，下一轮交出
        self._buffered: Any = _EMPTY
        #: 只读观测（排障/用例断言用；不进指标，避免为它扩标签白名单）
        self.approvals_emitted = 0
        self.heartbeats_emitted = 0

    # -- 节点侧 -------------------------------------------------------------

    async def request(self, payload: Dict[str, Any]) -> str:
        """节点侧挂起等待流内裁决；返回 ``"approve"`` / ``"reject"``。

        作为 ``review_interrupt`` 回调被 ``task_execution`` 调用（本方法是 async，
        ``task_execution`` 会 await 它——所以等待期事件循环照常转，心跳发得出去）。
        """
        loop = asyncio.get_running_loop()
        if self._pending is not None:
            # 单飞契约（task_execution 保证一次只有一个挂起）。越界调用 fail-closed：
            # 静默并排等待会让"批哪一个"变成不可判定。
            logger.error(
                "stream HITL: concurrent approval request for tool={} while another is "
                "pending -> reject (fail-closed)",
                payload.get("tool"),
            )
            return "reject"
        entry: Tuple[Dict[str, Any], "asyncio.Future[str]"] = (
            dict(payload or {}),
            loop.create_future(),
        )
        self._pending = entry
        self._announced = False
        self._wait_started = time.monotonic()
        self._opened.set()
        try:
            return await entry[1]
        finally:
            if self._pending is entry:
                self._pending = None
                self._announced = False
                self._opened.clear()

    # -- 流侧 ---------------------------------------------------------------

    async def next_item(
        self, anext: Callable[[], Awaitable[Any]]
    ) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
        """取下一个"项"：要么是图事件，要么是一帧要下发的审批/心跳帧。

        返回 ``(event, None)`` = 图事件；``(None, frame)`` = 要下发的帧
        （``frame["type"]`` 为 :data:`APPROVAL_FRAME_TYPE` 或
        :data:`HEARTBEAT_FRAME_TYPE`）。
        """
        while True:
            if self._buffered is not _EMPTY:
                buffered, self._buffered = self._buffered, _EMPTY
                if buffered is _STOP:
                    raise StopAsyncIteration
                return buffered, None

            entry = self._pending
            if entry is not None and not entry[1].done():
                frame = await self._waiting_tick(entry)
                if frame is not None:
                    return None, frame
                continue
            if entry is not None:
                # 裁决已落地、节点正在恢复：清槽后回到"等图事件"。
                self._pending = None
                self._announced = False
                self._opened.clear()

            item = await self._wait_event_or_request(anext)
            if item is _OPENED:
                continue
            if item is _STOP:
                raise StopAsyncIteration
            return item, None

    async def _waiting_tick(
        self, entry: Tuple[Dict[str, Any], "asyncio.Future[str]"]
    ) -> Optional[Dict[str, Any]]:
        """审批等待期的一拍：先宣告审批帧，再轮询裁决，再到点发心跳。"""
        payload, future = entry
        now = time.monotonic()
        if not self._announced:
            self._announced = True
            self._last_beat = now
            self.approvals_emitted += 1
            return self._frame(APPROVAL_FRAME_TYPE, self._approval_meta(payload, status="pending"))

        decision = await self._gate_decision(payload.get("request_id"))
        if decision is not None:
            if not future.done():
                future.set_result(decision)
            self.approvals_emitted += 1
            return self._frame(
                APPROVAL_FRAME_TYPE,
                self._approval_meta(payload, status="decided", decision=decision),
            )

        if now - self._last_beat >= self._heartbeat_s:
            self._last_beat = now
            self.heartbeats_emitted += 1
            return self._frame(
                HEARTBEAT_FRAME_TYPE,
                {
                    "status": "waiting_approval",
                    "request_id": payload.get("request_id"),
                    "tool": payload.get("tool"),
                    "waited_s": round(now - self._wait_started, 1),
                    "heartbeat_s": self._heartbeat_s,
                },
            )
        if (
            self._wait_cap_s is not None
            and now - self._wait_started > self._wait_cap_s
        ):
            # 兜底上限：显式 fail-closed（不是静默放行、也不是无限挂起）。
            # 这里只交出 ``reject`` 这个裁决值，**不替治理层写裁决**：节点侧
            # ``task_execution`` 的 reject 分支会 ``gate.reject(by="in_loop_resume",
            # note="in-loop reject")``——那条 pending 因此被**闭合**，归因是"流内等待
            # 上限触发的拒绝"（与 T19 到期拒的 ``system:deadline`` 不同源，见 §16）。
            logger.error(
                "stream HITL: no decision within {}s for request_id={} (tool={}) -> "
                "fail-closed reject",
                self._wait_cap_s,
                payload.get("request_id"),
                payload.get("tool"),
            )
            if not future.done():
                future.set_result("reject")
            return self._frame(
                APPROVAL_FRAME_TYPE,
                self._approval_meta(payload, status="decided", decision="reject"),
            )
        # 不到下一拍：短睡后复查（睡眠让出事件循环，图侧与裁决侧都能推进）。
        await asyncio.sleep(self._poll_s)
        return None

    async def _wait_event_or_request(
        self, anext: Callable[[], Awaitable[Any]]
    ) -> Any:
        """无待审时：等图事件、等审批请求，超 ``idle_timeout`` 抛 ``TimeoutError``。

        idle 语义与基线**同一把尺子**：只有"没有审批在等"的静默才计时
        （审批等待期由 :meth:`_waiting_tick` 的心跳占位，不进本函数）。
        """
        if self._anext_task is None:
            self._anext_task = asyncio.ensure_future(anext())
        opener: "asyncio.Future[Any]" = asyncio.ensure_future(self._opened.wait())
        try:
            done, _ = await asyncio.wait(
                {self._anext_task, opener},
                timeout=self._idle_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            opener.cancel()

        if self._opened.is_set():
            # 审批请求优先下发（前端要尽早看到可裁决面）。事件若同时到了先缓冲，
            # 下一轮原序交出——顺序不丢。
            self._opened.clear()
            if self._anext_task is not None and self._anext_task.done():
                self._buffered = self._result(self._anext_task)
                self._anext_task = None
            return _OPENED
        if self._anext_task is not None and self._anext_task in done:
            result = self._result(self._anext_task)
            self._anext_task = None
            return result
        # 静默超时：与基线 ``asyncio.wait_for`` 一样取消在飞的 ``__anext__``，
        # 否则随后的 ``stream_iter.aclose()`` 会撞
        # "asynchronous generator is already running"。
        self._cancel_anext()
        raise asyncio.TimeoutError()

    # -- 收尾 ---------------------------------------------------------------

    def close(self) -> None:
        """流收尾清理（幂等）：取消在飞的取事件任务与未决等待。

        必须在 ``stream_iter.aclose()`` **之前**调用（原因见
        :meth:`_wait_event_or_request` 的 idle 分支）。
        """
        self._cancel_anext()
        entry, self._pending = self._pending, None
        self._announced = False
        self._buffered = _EMPTY
        self._opened.clear()
        if entry is not None and not entry[1].done():
            entry[1].cancel()

    def _cancel_anext(self) -> None:
        task, self._anext_task = self._anext_task, None
        if task is not None and not task.done():
            task.cancel()

    # -- 内部 ---------------------------------------------------------------

    @staticmethod
    def _result(task: "asyncio.Future[Any]") -> Any:
        """取 ``__anext__`` 结果：``StopAsyncIteration`` 归一成占位，其余原样抛。"""
        try:
            return task.result()
        except StopAsyncIteration:
            return _STOP

    async def _gate_decision(self, request_id: Any) -> Optional[str]:
        """裁决判定：**本地闸门快路径 → 未决时回捞 T18 库行**。

        **判定只有一个源**（纪律 6 ④）：裁决事实写在 T18 的
        ``hitl_pending_approvals`` 行上——``bridge.decide`` 与 T19 到期拒都闭合
        同一行；本地闸门簿记是同一事实的**进程内快路径**。所以先读快路径，
        未决（仍是 ``PENDING``／本进程压根没有这条记录）时按 ``sidecar_request_id``
        回捞同一条库记录：两处读的是**同一份裁决**，回捞不引入第二个判定源。

        回捞为什么必要：默认部署是 ``--workers 2``，``/ops/hitl/approvals/{id}``
        的裁决落在**哪个 worker 不确定**。裁决落在另一个 worker 时，本进程的闸门
        记录永远停在 ``PENDING``——只读快路径的话，这条流会一直等到
        ``AGENT_TIMEOUT`` 以"处理请求超时"收口，那是超时、不是治理终局。

        回捞频率 = 既有 ``DECISION_POLL_S``（不新增配置键）；flag-off
        （``HITL_PENDING_PERSIST_ENABLED=false``）时 :meth:`_resolve_store` 返回
        ``None``，连 store 模块都不 import——与基线等价，零额外读库。
        """
        if not request_id:
            return None
        local = self._local_decision(str(request_id))
        if local is not None:
            return local
        return await self._store_decision(request_id)

    @staticmethod
    def _local_decision(request_id: str) -> Optional[str]:
        """快路径：本进程闸门簿记（同进程裁决 / T19 在**本进程**扫到时写这里）。"""
        from agentix.tools.gateway.human_review import (  # 惰性：避开导入环
            ReviewDecision,
            get_review_gate,
        )

        request = get_review_gate().get(request_id)
        if request is None:
            return None
        if request.decision is ReviewDecision.APPROVED:
            return "approve"
        if request.decision is ReviewDecision.REJECTED:
            return "reject"
        return None

    async def _store_decision(self, request_id: Any) -> Optional[str]:
        """回捞 T18 库行：本进程看不见的那次跨 worker 裁决只有库里是终态。

        失败姿态（纪律 2 fail-open）：库面不可用 / 行读不出来 / 映射不到
        ``sidecar_request_id`` ⇒ **当作未命中**（返回 ``None``，本地闸门照旧轮询），
        ``logger.warning`` 留痕；绝不把异常抛进取事件循环，也绝不因此提前收口——
        回捞只是快路径的补充，不是等待的收口条件。
        """
        store = self._resolve_store()
        if store is None:
            return None
        sidecar_id = self._sidecar_id(request_id)
        if not sidecar_id:
            # 闸门 id → sidecar id 的映射只在**创建这条 pending 的那个实例**的内存里
            # （桥的 `_map`）。查不到就是查不到：不猜 id、不扫全表。
            return None
        try:
            row = await asyncio.to_thread(store.load, str(sidecar_id))
        except Exception as exc:  # noqa: BLE001 - 降级去向：当作未命中，本地闸门照旧轮询（回捞失败不得收口、不得抛进流）
            if not self._store_warned:
                self._store_warned = True
                logger.warning(
                    "stream HITL: pending store load failed for sidecar_request_id={} "
                    "(falling back to in-process gate polling): {}",
                    sidecar_id,
                    exc,
                )
            return None
        return _STORE_STATUS_DECISION.get(str((row or {}).get("status") or ""))

    def _resolve_store(self) -> Optional[Any]:
        """T18 库面（回捞用）：判据唯一源是 ``hitl_bridge.resolve_pending_store``。

        惰性解析一次并缓存：本模块**不另读 flag**（纪律 6 ④）。flag-off 时那个
        函数连 store 模块都不 import（不触 sqlalchemy、不建池、不建表）⇒ 这里拿到
        ``None`` 是"本卡没开落库"这个合法结果，不是故障。
        """
        if self._store is _STORE_UNSET:
            try:
                from agentix.policies.hitl.hitl_bridge import resolve_pending_store

                self._store = resolve_pending_store()
            except Exception as exc:  # noqa: BLE001 - 降级去向：无库面 ⇒ 只读本地闸门（回捞不可用，等待与心跳照旧）
                logger.warning(
                    "stream HITL: pending store unavailable for this stream "
                    "(cross-worker decisions will not be picked up): {}",
                    exc,
                )
                self._store = None
        return self._store

    @staticmethod
    def _approval_meta(
        payload: Dict[str, Any],
        *,
        status: str,
        decision: Optional[str] = None,
    ) -> Dict[str, Any]:
        """审批帧 ``metadata``：只放裁决所需的最小面（不回显工具参数）。"""
        gate_id = payload.get("request_id")
        sidecar_id = StreamHitlChannel._sidecar_id(gate_id)
        meta: Dict[str, Any] = {
            "status": status,
            "request_id": gate_id,
            "sidecar_request_id": sidecar_id,
            "tool": payload.get("tool"),
            "risk_level": payload.get("risk_level"),
            "reason": "review_required",
        }
        if sidecar_id:
            # 裁决入口由服务端给出（前缀唯一真源是 settings.API_V1_PREFIX），
            # 前端不自己拼路径。
            meta["decide_url"] = (
                f"{settings.API_V1_PREFIX}/ops/hitl/approvals/{sidecar_id}"
            )
        if decision is not None:
            meta["decision"] = decision
        return meta

    @staticmethod
    def _sidecar_id(gate_id: Any) -> Optional[str]:
        """闸门请求 id → sidecar 请求 id（裁决 API 的入参是后者）。

        只在决策面已接线时查桥（``decision_surface_active()``）——否则
        ``get_hitl_bridge()`` 会顺手创建一个没人用的桥，属于装配副作用。
        查不到就**不带**该字段（前端退回 ``GET /ops/hitl/approvals`` 取 id），
        不伪造一个假 id 让裁决打到空气里。
        """
        if not gate_id:
            return None
        try:
            from agentix.tools.gateway.human_review import decision_surface_active

            if not decision_surface_active():
                return None
            from agentix.policies.hitl.hitl_bridge import get_hitl_bridge

            return get_hitl_bridge().sidecar_id_for_gate(str(gate_id))
        except Exception as exc:  # noqa: BLE001 - 降级去向：帧照发但不带 sidecar id（前端可经 GET /ops/hitl/approvals 兜底）
            logger.debug("stream HITL: sidecar id lookup failed: {}", exc)
            return None

    @staticmethod
    def _frame(frame_type: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """SSE 帧骨架（``type`` 是唯一词汇，A5/R5b；``session_id`` 由调用方补）。"""
        return {"type": frame_type, "metadata": metadata}
