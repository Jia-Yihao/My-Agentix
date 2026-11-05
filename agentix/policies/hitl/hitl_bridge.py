"""HITL 三栈库级桥（Phase 16）：Gate → Sidecar → WAITING_APPROVAL/breakpoint 单线贯通。

背景（规划 §5）：三套 HITL 各自测通却互不连通——

- 栈A ``capabilities.tools.human_review.HumanReviewGate``：钉在
  ``ToolExecutor`` 执行点（fail-closed），拦下只留一条
  ``reason="review_required"`` 的结果，**没人能批**；
- 栈B ``agent.hitl.sidecar.ReviewerSidecar``：四裁决 + 上下文隔离审面，
  **零生产调用方**；
- 栈C ``breakpoint_resume``（``InterruptReason.HUMAN_APPROVAL``）与
  ``AgentStatus.WAITING_APPROVAL``：审批/断点状态面，与前两者无连接。

本桥把三者接成一条线（**库级**；图内 ``interrupt()`` 按规划单列评估）::

    ToolExecutor 拦下（review_required + 原始 args）
      → bridge.open_from_gate(request, args)          # 栈A → 栈B
          SidecarReviewRequest.submit（WAITING_APPROVAL 面）
          可选 state → AgentStatus.WAITING_APPROVAL
          可选 breakpoint InterruptRecord(reason=HUMAN_APPROVAL)
      → bridge.decide(id, APPROVE|REJECT|EDIT|RESUME)
          sidecar.review（approver fail-closed）→ 映射回 Gate.approve/reject
          EDIT：merged = raw ∪ edits；审批记录 digest/risk_level 按
          preflight 同式重算 → 执行点带 merged 重试即可放行
      → 重启恢复：to_persistence / from_persistence（Sidecar pending +
          id 映射 + Gate 请求记录）

**生产接缝**：``ToolExecutor(review_blocked_hook=...)``（默认 ``None`` =
零变化）；``main`` 仅在 ``settings.HUMAN_REVIEW_ENABLED=true`` 时经
:func:`install_executor_hook` 挂上——**默认关 = 不接线 = 字节等价**。

**观测**（DoD「误拒可观测，不能只记拦截数」）：每次挂起
``record_approval_created``、每次裁决 ``record_approval_decision``；
:meth:`HitlBridge.stats` 给出 created/approved/rejected/… 与
``rejection_rate``（拒批占比 = 误拒率的可观测分子分母；**真·误拒率**
需要人工复核标注才能算，库内不伪造）。

**Permission modes**（Claude Code 多模式的简化子集，收敛到本桥配置面）：

- ``default``：拦下必批（挂起等人工裁决）；
- ``accept_edits``：编辑类工具名且**无参数提权、无声明级参与**时自动放行
  （留审计）；否则同 ``default``；
- ``bypass``：操作员显式绕过（立即批准，留审计）。
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from loguru import logger

from agentix.policies.hitl.sidecar import (
    ReviewDecisionAction,
    ReviewerSidecar,
    SidecarReviewRequest,
)
from agentix.tools.definitions import registry as tool_registry
from agentix.tools.gateway.human_review import (
    HumanReviewGate,
    ReviewDecision,
    ReviewRequest,
    RiskLevel,
    classify_risk,
    digest_args,
    effective_risk_level,
    get_review_gate,
    rank_of,
)
# M0 shim：PermissionMode 规范位已迁 domain/tool/permission.py（对象同一，M6 清尾）
from agentix.domain.tool.permission import PermissionMode
from agentix.observability.metrics import (
    record_approval_created,
    record_approval_decision,
    record_approval_timeout,
)
from agentix.runtime.lifecycle import LifecycleManager
from agentix.runtime.state import AgentState, AgentStatus

__all__ = [
    "PermissionMode",
    "AUTO_REJECT_TIMEOUT_REASON",
    "AUTO_REJECT_TIMEOUT_ACTOR",
    "BridgeSuspend",
    "BridgeDecision",
    "HitlBridge",
    "get_hitl_bridge",
    "reset_hitl_bridge",
    "install_executor_hook",
    "resolve_pending_store",
    "resolve_approval_deadline_s",
]

#: 审批有效级 → Sidecar 声明级（与 human_review._L_TO_RISK 互逆的唯一桥内映射）
_RISK_TO_L: Dict[RiskLevel, str] = {
    RiskLevel.LOW: "L0",
    RiskLevel.MEDIUM: "L1",
    RiskLevel.HIGH: "L2",
    RiskLevel.CRITICAL: "L3",
}

#: ``accept_edits`` 自动放行的编辑类工具名模式
_EDIT_NAME_RE = re.compile(r"(?i)(edit|write|update|insert|upsert|patch|modify|create)")

#: T19 超时拒的归因取值（三处可见字段共用同一个字面量，不许各自拼一份）：
#: ``BridgeDecision.reason``、库行 ``decision_reason`` 与 ``stats()["auto_reject_timeout"]``。
AUTO_REJECT_TIMEOUT_REASON = "auto_reject_timeout"
#: T19 自动拒绝的裁决人标记（库行 ``decided_by``）：人工拒是 ``by="ops"`` 之类，
#: 两者必须能一眼分开——"谁拒的"是本卡最容易混写的归因。
AUTO_REJECT_TIMEOUT_ACTOR = "system:deadline"

#: 内部错误标记：闸门里已没有待决记录 ⇒ 这次拒绝没有新落地（T19 第 2 轮返工）。
#: 不进 ``__all__``：它是 ``expire_overdue`` 与 ``decide`` 之间的私有约定，
#: 不是对外契约的一部分。
_GATE_NOT_PENDING_ERROR = "gate_request_not_pending"


@dataclass(frozen=True)
class BridgeSuspend:
    """一次挂起的结果（也是 executor 拦截钩子的返回值）。"""

    gate_request_id: str
    #: 自动放行路径为 ""（未挂 Sidecar）
    sidecar_request_id: str
    mode: str
    #: 钩子是否已代为批准（executor 据此"再闸一次"）
    approved: bool
    auto: bool
    #: T18 验收⑤：pending 落库失败的**可见标记**（空串 = 无故障）。
    #: store=None（flag-off 基线）时恒空串——回执形状不变、行为逐位等价。
    persist_error: str = ""


@dataclass(frozen=True)
class BridgeDecision:
    """一次裁决的结果。"""

    gate_request_id: str
    sidecar_request_id: str
    #: 请求的裁决动作
    action: ReviewDecisionAction
    #: 实际落地的动作（approver 否决时为 REJECT）
    result_action: ReviewDecisionAction
    approved: bool
    #: EDIT 合并后的参数（供执行点重试）；其余动作为 None
    merged_arguments: Optional[Dict[str, Any]] = None
    reason: str = ""
    not_found: bool = False
    error: str = ""
    #: 重启恢复后闸门记录被重建（原记录丢失/未恢复）
    gate_recovered: bool = False
    #: T18 验收⑤：裁决已落闸门但**库闭合失败**的可见标记（空串 = 无故障）。
    #: 治理语义不变（闸门照常批/拒），但"库里那条还挂着"必须让调用方看得见。
    persist_error: str = ""


class HitlBridge:
    """三栈桥：Gate（风险有效级）→ Sidecar（四裁决）→ 状态/断点面。

    构造参数全部可注入（测试/运维）；缺省 ``gate`` 取全局单例
    （由 ``settings.HUMAN_REVIEW_ENABLED`` 决定开关），``breakpoints``
    为 ``None`` 时**不碰 Redis**（离线可用）。
    """

    def __init__(
        self,
        *,
        gate: Optional[HumanReviewGate] = None,
        sidecar: Optional[ReviewerSidecar] = None,
        breakpoints: Optional[Any] = None,
        permission_mode: PermissionMode = PermissionMode.MANUAL,
        pending_store: Optional[Any] = None,
        approval_deadline_s: float = 0.0,
    ) -> None:
        self._gate: HumanReviewGate = gate if gate is not None else get_review_gate()
        self._sidecar: ReviewerSidecar = sidecar if sidecar is not None else ReviewerSidecar()
        #: BreakpointManager（需有 ``record_interrupt``/``resolve_interrupt``）；可为 None
        self._breakpoints = breakpoints
        self._mode = PermissionMode(permission_mode)
        #: T18（Q7 裁决：库为真源）pending 落库面（``HitlPendingStore`` 鸭子型）。
        #: None = 纯内存基线——**flag-off 时装配判定保证它是 None**（约定 1 逐位等价）；
        #: 库侧未决上限 200 与内存 FIFO 同数（口径唯一真源见 hitl_pending_store 模块注释），
        #: 溢出显式拒绝、不静默挤行。
        self._pending_store = pending_store
        #: T19（Q9 裁决：到期自动拒绝）审批等待时限（秒）。0 = 关 = 现行为。
        #: 本值只用于"这台桥有没有超时语义"（决定统计键是否出现）与运维可读；
        #: 真正的扫描期限由调用方（``main._hitl_deadline_sweeper``）每次传进来，
        #: 不在这里再读一次 settings——配置只有一个读取点，调用方经
        #: :func:`resolve_approval_deadline_s` 取同一个值（同一个函数，不是同一个
        #: 字符串常量各拼一份），测试也只需注入一处。
        self._approval_deadline_s = float(approval_deadline_s or 0.0)
        #: T19 第 2 轮返工：扫描租约 / 行认领的持有者标识。
        #: 要求只有两条：**进程内稳定**（同一台桥的每一轮扫描必须是同一个 owner，
        #: 否则续租与"释放自己的认领"都失效）+ **跨进程可区分**（host:pid 即可，
        #: 再加 8 位随机尾巴区分同机同 pid 复用的陈旧行）。刻意不用"每个进程
        #: 一个配置项"：那会多出一个能配错的地方，而这个值没有任何运维语义。
        self._sweep_owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        #: sidecar_request_id → gate_request_id
        self._map: Dict[str, str] = {}
        #: sidecar_request_id → 桥自留的 Sidecar 请求副本（含原始 args）
        self._requests: Dict[str, SidecarReviewRequest] = {}
        #: ``_requests`` 唯一的互斥锁（第 3 轮返工）：``pending()`` 经
        #: ``asyncio.to_thread`` 在工作线程迭代，而挂起/裁决/回捞在事件循环
        #: 线程变异同一个 dict——不加锁就是 "dictionary changed size during
        #: iteration"。锁**只护内存面**：绝不持锁跨 DB I/O，也绝不嵌套
        #: （非重入锁，嵌套即自死锁）。
        self._lock = threading.Lock()
        #: 打开过的 gate 请求 id（持久化时据此回捞闸门记录）
        self._seen_gate_ids: List[str] = []
        self._counts: Dict[str, int] = {
            "created": 0,
            "approved": 0,
            "rejected": 0,
            "edited": 0,
            "resumed": 0,
            "auto": 0,
            "bypass": 0,
            "not_found": 0,
        }
        if pending_store is not None:
            # 故障计数键**只在库面装配时存在**：flag-off 的 stats() 键集合必须与
            # 基线逐位相同（约定 1），所以不预先塞 0。
            self._counts["persist_errors"] = 0
            self._counts["restore_errors"] = 0
        if self._approval_deadline_s > 0:
            # 同上：超时拒的计数键**只在 deadline>0 时存在**。deadline=0（默认）连
            # 这个键都不出现，stats() 键集合与基线逐字相同（约定 1）。
            self._counts[AUTO_REJECT_TIMEOUT_REASON] = 0

    # -- 栈A → 栈B：挂起 --------------------------------------------------

    async def open_from_gate(
        self,
        request: ReviewRequest,
        args: Optional[Dict[str, Any]] = None,
        *,
        thread_id: str = "",
        node_name: str = "tool",
        state: Optional[AgentState] = None,
    ) -> BridgeSuspend:
        """把闸门拦下的请求转成 Sidecar 待审面（executor 拦截钩子的入口）。

        ``args`` 为本次调用的**原始参数**（缺省回退 ``request.args_digest``，
        长值可能已被截断）。返回值 ``approved=True`` 表示 permission_mode
        已代为批准（executor 会再闸一次直接放行）。

        本方法即 executor 拦截钩子（签名 ``(request, args) -> awaitable``）。
        钩子路径拿不到 ``AgentState``：状态挂起由持有 state 的调用方
        （图/循环层）显式传 ``state=`` 完成。
        """
        if request is None:
            raise ValueError("open_from_gate requires a ReviewRequest")
        raw_args: Dict[str, Any] = dict(args) if args is not None else dict(request.args_digest)
        gate_id = request.request_id
        if gate_id not in self._seen_gate_ids:
            self._seen_gate_ids.append(gate_id)

        # 拦截即记（无论后续走哪条 permission_mode 路径）
        try:
            record_approval_created(target_type="tool")
        except Exception as exc:  # noqa: BLE001 - 指标故障不影响审批链
            logger.debug("record_approval_created failed: {}", exc)
        self._counts["created"] += 1

        # -- 自动放行路径（bypass / accept_edits）--------------------------
        auto_label: Optional[str] = None
        if self._mode is PermissionMode.BYPASS:
            auto_label = "bypass"
        elif self._mode is PermissionMode.ACCEPT_EDITS and self._accept_edits_auto(
            request, raw_args
        ):
            auto_label = "auto"
        if auto_label is not None:
            ok = self._gate.approve(
                gate_id,
                by=f"permission_mode:{auto_label}",
                note=f"HITL bridge auto-approved by mode={self._mode.value}",
            )
            self._counts[auto_label] += 1
            self._record_decision(auto_label)
            logger.info(
                "HITL bridge {} gate request {} (tool={}, mode={})",
                auto_label,
                gate_id,
                request.tool_name,
                self._mode.value,
            )
            return BridgeSuspend(
                gate_request_id=gate_id,
                sidecar_request_id="",
                mode=self._mode.value,
                approved=bool(ok),
                auto=True,
            )

        # -- 手工挂起（default / accept_edits 未命中）------------------------
        side_req = SidecarReviewRequest.create(
            tool_name=request.tool_name,
            arguments=raw_args,
            declared_risk_level=_RISK_TO_L.get(request.risk_level, "L3"),
        )
        await self._sidecar.submit(side_req)
        # 变异必须在锁下：与 pending() 工作线程的快照迭代互斥（见 _lock 注释）
        with self._lock:
            self._map[side_req.request_id] = gate_id
            self._requests[side_req.request_id] = side_req
        self._transition(state, AgentStatus.WAITING_APPROVAL)
        await self._record_interrupt(side_req.request_id, thread_id, node_name, request)

        # T18 验收①：挂起必须落库——库是真源（Q7 裁决），重启与跨实例裁决都吃这一行。
        # 写库时序刻意放在内存挂起**之后**：先保证审批链不断，再补真源。
        # 失败姿态（验收⑤）：error 日志 + persist_errors 计数 + 回执 persist_error 标记——
        # 内存挂起仍可用（阻断它反而劣于基线），但绝不"纯内存还宣称成功"。
        persist_error = ""
        if self._pending_store is not None:
            try:
                # 同步 repo（本包 CRUD 惯例）经 to_thread 下放，不卡事件循环（约定 6 ②）
                await asyncio.to_thread(
                    self._pending_store.save_pending,
                    sidecar=side_req.to_public_dict(),
                    gate_request=request.to_dict(),
                    thread_id=thread_id,
                )
            except Exception as exc:  # noqa: BLE001 - 降级去向：内存挂起保留 + 回执可见标记 + error 日志 + 计数
                persist_error = f"pending_persist_failed: {exc}"
                self._counts["persist_errors"] += 1
                logger.error(
                    "HITL pending persist FAILED for sidecar {} (gate {}): this pending "
                    "lives in memory only until the store write recovers: {}",
                    side_req.request_id,
                    gate_id,
                    exc,
                )
        logger.info(
            "HITL bridge suspended gate request {} as sidecar {} (tool={}, risk={})",
            gate_id,
            side_req.request_id,
            request.tool_name,
            request.risk_level.value,
        )
        return BridgeSuspend(
            gate_request_id=gate_id,
            sidecar_request_id=side_req.request_id,
            mode=self._mode.value,
            approved=False,
            auto=False,
            persist_error=persist_error,
        )

    # -- 栈B → 栈A：四裁决 ------------------------------------------------

    async def decide(
        self,
        sidecar_request_id: str,
        action: ReviewDecisionAction,
        *,
        edited_arguments: Optional[Dict[str, Any]] = None,
        reason: str = "",
        by: str = "human",
        state: Optional[AgentState] = None,
        resume_status: AgentStatus = AgentStatus.RUNNING,
        attribution: Optional[str] = None,
        require_gate_pending: bool = False,
    ) -> BridgeDecision:
        """执行四裁决并落回闸门。

        - ``APPROVE`` / ``RESUME`` → ``gate.approve``（下次执行即消费）；
        - ``EDIT`` → 合并 edits，按 preflight 同式重算审批记录的
          ``args_digest`` / ``risk_level``（编辑后 scope 精确等于人工所批），
          返回 ``merged_arguments`` 供执行点重试；
        - ``REJECT``（含 Sidecar approver 否决）→ ``gate.reject``（fail-closed）。

        ``attribution`` 是**内部**参数，仅供 :meth:`expire_overdue` 传入超时归因：
        子集计数只在这一次写入处 +1，避免事后回改（先 +1 再 -1 会开出一段
        "读数窗口内两桶都不对"的空窗）。它**不改变**裁决桶本身——``rejected``
        仍然计所有拒绝（人工 + 超时），指标打点仍是 ``decision="rejected"``。
        ``attribution`` 只对拒绝有意义（它唯一的取值是超时拒归因）：批准路径传
        归因等于给一次批准贴上"超时拒"的标签，是错的口径 ⇒ 显式拒绝而不是忽略。

        ``require_gate_pending``（内部参数，默认 ``False``）只有
        :meth:`expire_overdue` 传 ``True``：要求这次拒绝**在闸门侧真的新落地**。
        闸门里那条已是终态时 ``gate.reject`` 返回 ``False``（``_resolve`` 的
        既有语义），此时若照常计数/闭合，同一次裁决会被数两遍——扫描者之间、
        以及"闸门已拒但库闭合失败后重扫"都会走到这里。默认 ``False`` 是为了让
        人工裁决与 flag-off 路径行为逐位不变。

        **内存命中也会先核一次库行 status**（T19 第 4 轮返工）：多 worker 部署下同一
        条 pending 可能已被对端实例裁决，而本实例的内存副本没人清理。多这一次库读
        换的是"裁决只写一次"——否则后手会把先手的 ``decided_by`` / ``decision_reason``
        / ``status`` 一起改写，超时归因被人工裁决抹掉。库行已闭合 ⇒ ``not_found``
        （``reason="pending_already_closed"``，并清掉本实例陈旧副本）；**读失败同样按
        ``not_found`` 收口**（``reason="pending_status_unreadable"``）——与上面跨实例
        回捞"读不到绝不放行"同一条 fail-closed 口径，不给"读不到就当它还能裁"留缝。
        """
        action = ReviewDecisionAction(action)
        if attribution is not None and action is not ReviewDecisionAction.REJECT:
            raise ValueError("attribution is only valid for REJECT decisions (timeout attribution)")
        gate_id = self._map.get(sidecar_request_id)
        request = self._requests.get(sidecar_request_id)
        if self._pending_store is not None:
            if gate_id is None or request is None:
                # T18 验收③ 跨实例：本实例内存没有这条 pending ≠ 它不存在——库是真源，
                # 先回捞再判。回捞失败（读库故障/坏行）只 error 留痕并**维持 not_found**
                # 的 fail-closed 兜底：读不到绝不放行（治理层语义不因落库而放宽）。
                try:
                    row = await asyncio.to_thread(self._pending_store.load, sidecar_request_id)
                    self._apply_loaded_pending(row)
                except Exception as exc:  # noqa: BLE001 - 降级去向：维持 not_found（fail-closed），error 日志可观察
                    logger.error(
                        "HITL pending store load failed for {} (staying not_found, decision NOT made): {}",
                        sidecar_request_id,
                        exc,
                    )
                gate_id = self._map.get(sidecar_request_id)
                request = self._requests.get(sidecar_request_id)
            else:
                # 内存命中 ≠ 仍待决：库是真源。对端实例（``--workers 2`` 下扫描租约只在
                # 一个 worker 上）或本实例上一轮可能已经把这一行闭合了，而内存副本
                # 没人清理。若照内存状态再裁一次，``close_decision`` 会把
                # decided_by / decision_reason / status 全部改写——超时归因就此丢失
                #（点 approve 连 rejected 都会变 approved）。所以多读这一次库：一次
                # 库读换"归因不被覆盖"，相对一次人工裁决的代价可以忽略。
                # 读失败按 not_found 收口，与上面"读不到绝不放行"同口径；``row is None``
                # （从未落库成功）**不阻止**裁决——close_decision 会补写这一行。
                try:
                    row = await asyncio.to_thread(self._pending_store.load, sidecar_request_id)
                except Exception as exc:  # noqa: BLE001 - 降级去向：维持 not_found（fail-closed），error 日志可观察
                    logger.error(
                        "HITL pending status check failed for {} (staying not_found, "
                        "decision NOT made): {}",
                        sidecar_request_id,
                        exc,
                    )
                    self._counts["not_found"] += 1
                    return BridgeDecision(
                        gate_request_id=gate_id,
                        sidecar_request_id=sidecar_request_id,
                        action=action,
                        result_action=action,
                        approved=False,
                        not_found=True,
                        reason="pending_status_unreadable",
                    )
                if row is not None and row.get("status") != "pending":
                    # 库行已闭合：裁决已在别处落过（裁决只写一次）。清掉本实例的陈旧
                    # 副本，免得待审面与裁决面各说一套。
                    with self._lock:
                        self._map.pop(sidecar_request_id, None)
                        self._requests.pop(sidecar_request_id, None)
                    self._counts["not_found"] += 1
                    return BridgeDecision(
                        gate_request_id=gate_id,
                        sidecar_request_id=sidecar_request_id,
                        action=action,
                        result_action=action,
                        approved=False,
                        not_found=True,
                        reason="pending_already_closed",
                    )
        if gate_id is None or request is None:
            self._counts["not_found"] += 1
            return BridgeDecision(
                gate_request_id="",
                sidecar_request_id=sidecar_request_id,
                action=action,
                result_action=action,
                approved=False,
                not_found=True,
                reason="unknown_sidecar_request",
            )

        try:
            result = await self._sidecar.review(
                request,
                action=action,
                edited_arguments=edited_arguments,
                reason=reason,
            )
        except ValueError as exc:
            # EDIT 未带 edited_arguments 等参数错误：原样回传，不落任何判定
            return BridgeDecision(
                gate_request_id=gate_id,
                sidecar_request_id=sidecar_request_id,
                action=action,
                result_action=action,
                approved=False,
                error=str(exc),
            )

        approved = result.action in (
            ReviewDecisionAction.APPROVE,
            ReviewDecisionAction.EDIT,
            ReviewDecisionAction.RESUME,
        )
        merged: Optional[Dict[str, Any]] = None
        if result.action is ReviewDecisionAction.EDIT:
            merged = {**request.arguments, **(result.edited_arguments or {})}

        gate_recovered = False
        if approved:
            gr = self._gate.get(gate_id)
            if gr is None:
                # 重启恢复缺口：闸门记录丢失/未恢复 → 按 Sidecar 载荷重建
                # 已决记录（scope 与 preflight 同式计算，不放宽任何匹配）
                gr = self._rebuild_gate_request(gate_id, request, approved=True)
                self._gate.restore_request(gr)
                gate_recovered = True
            if merged is not None:
                gr.args_digest = digest_args(merged)
                gr.risk_level = self._effective_level_for(gr.tool_name, merged)
            self._gate.approve(
                gate_id,
                by=by,
                note=result.reason or f"hitl-bridge:{result.action.value}",
            )
        else:
            landed = self._gate.reject(gate_id, by=by, note=result.reason or "hitl-bridge:reject")
            # 闸门里已没有待决记录 ⇒ 这次拒绝**没有新落地**（``_resolve`` 对不存在的
            # 记录与已决记录都返回 False）。此时继续往下走就会把同一次裁决数两遍：
            # 计数 +1、库闭合再写一遍、超时指标再打一次。只对调用方声明
            # ``require_gate_pending`` 的路径（``expire_overdue``）短路；人工裁决
            # 维持既有行为（那里"已决再拒"本来就是幂等收口，改它会改人工口径）。
            if require_gate_pending and not landed:
                return BridgeDecision(
                    gate_request_id=gate_id,
                    sidecar_request_id=sidecar_request_id,
                    action=action,
                    result_action=result.action,
                    approved=False,
                    reason=result.reason,
                    error=_GATE_NOT_PENDING_ERROR,
                )

        # 裁决后：释放挂起（映射与副本不再需要；闸门记录留待执行点消费）
        # 变异与挂起登记同源同锁——pop 也在锁下，保证 pending() 快照不漏已决行
        with self._lock:
            self._map.pop(sidecar_request_id, None)
            self._requests.pop(sidecar_request_id, None)

        # 值域收敛（维护者 2026-10-27 裁决；第 1 轮审查由五值收敛为六值）：
        # 指标 decision 只允许 {approved, rejected, edited, resumed, auto,
        # bypass}，故打点取映射值而非枚举原值（``result.action.value`` 会把
        # approve/reject/edit/resume 漏进序列，与进环/检索侧不同源）。
        # ``bypass`` 单列：它是 PermissionMode.BYPASS 的模式级豁免（见
        # :211→:223 的 auto_label 写入），与逐请求自动放行 ``auto`` 治理含义
        # 不同——归并会抹掉"谁批的"这层区分。映射先于打点定义，计数与打点同源。
        bucket = {
            ReviewDecisionAction.APPROVE: "approved",
            ReviewDecisionAction.REJECT: "rejected",
            ReviewDecisionAction.EDIT: "edited",
            ReviewDecisionAction.RESUME: "resumed",
        }
        self._record_decision(bucket[result.action])
        self._counts[bucket[result.action]] += 1
        if attribution:
            # 前提：``attribution`` 只由扫描侧（:meth:`expire_overdue`）传入，而桥的
            # ``approval_deadline_s > 0`` 时这个计数键才存在（约定 1：deadline=0 的
            # ``stats()`` 键集合必须与基线逐字相同）。键缺失 = 桥配置与扫描侧不一致
            # （例如 deadline=0 的桥被外部强行驱动了扫描），**不能静默跳过**：那会让
            # "超时拒"这一桶永远停在 0 而无人察觉。
            if attribution in self._counts:
                self._counts[attribution] += 1
            else:
                logger.warning(
                    "HITL bridge attribution key {} missing (bridge approval_deadline_s={} "
                    "so the timeout sub-count bucket was never initialized, yet a timed-out "
                    "decision reached decide()); sub-count NOT incremented",
                    attribution,
                    self._approval_deadline_s,
                )

        # T18 验收①：裁决闭合**同库同一行**（status=裁决桶标签，与库状态词表同源）。
        # 失败姿态（验收⑤）：闸门裁决已按治理语义落地，但库未闭合 → error 日志 +
        # 计数 + 回执 persist_error 标记——"库里还挂着"对运维可见，不谎报完全成功。
        persist_error = ""
        if self._pending_store is not None:
            try:
                closed = await asyncio.to_thread(
                    self._pending_store.close_decision,
                    sidecar_request_id,
                    status=bucket[result.action],
                    decided_by=by,
                    reason=result.reason,
                    edited_arguments=merged,
                    # 补写依据：pending 生于 flag 切换窗口时库无行，裁决仍必须进真源
                    sidecar=request.to_public_dict(),
                )
            except Exception as exc:  # noqa: BLE001 - 降级去向：闸门判定不回滚（治理终局），库闭合失败以回执标记+error 日志可见
                persist_error = f"pending_close_failed: {exc}"
                self._counts["persist_errors"] += 1
                logger.error(
                    "HITL pending close failed in store for {} (gate decision already "
                    "landed, db row NOT closed): {}",
                    sidecar_request_id,
                    exc,
                )
            else:
                if closed is False:
                    # 已决行不覆盖（CAS）：本次库闭合只"没写成"，不是故障。上面的
                    # status 预读与这里的写入之间存在窗口，对端实例在窗口内闭合是
                    # 正常竞态；闸门裁决与计数已按本实例口径落地，故这里只告警、
                    # 不回落 persist_error（它只表示抛错），也不改任何计数。
                    logger.warning(
                        "HITL pending row {} was already closed by a peer instance "
                        "(decisions are written once), so this instance's db close did NOT "
                        "land; the gate-side decision was already counted per this instance",
                        sidecar_request_id,
                    )

        if state is not None and state.status is AgentStatus.WAITING_APPROVAL:
            self._transition(state, resume_status)
        await self._resolve_interrupt(sidecar_request_id, result.action, merged)

        return BridgeDecision(
            gate_request_id=gate_id,
            sidecar_request_id=sidecar_request_id,
            action=action,
            result_action=result.action,
            approved=approved,
            merged_arguments=merged,
            reason=result.reason,
            gate_recovered=gate_recovered,
            persist_error=persist_error,
        )

    # -- 查询 / 观测 -------------------------------------------------------

    def pending(self) -> List[Dict[str, Any]]:
        """待审面投影（给审批 UI；**无** Agent trajectory / narrative）。

        T18 验收③：装配库面时并入库中未决而内存没有的行——跨实例的 GET 也要能
        点名（否则运维在 B 实例上根本拿不到 pending id 去裁决）。读库失败只
        error 留痕、内存面照常返回（查询降级不放大故障；裁决路径另有 fail-closed 兜底）。

        T19 第 4 轮返工：装配库面时**先按库行 status 收敛内存面**。跨实例裁决后本
        实例的内存副本可能陈旧（``--workers 2`` 下扫描租约只让一个 worker 扫，被拒的
        那条 pending 在对端实例里被闭合，这边的 ``_requests`` 没人清理），不收敛的话
        审批列表会一直挂着一条已超时拒绝的行、且能对它再点一次裁决。**待审面以库行
        status 为准（库是真源）**；库里没有该行（从未落库成功的内存副本）照旧列出。
        剔除的同时把这批陈旧副本从 ``_map`` / ``_requests`` 里**回收掉**：``stats()``
        的 ``pending`` 读的是 ``len(_requests)``，只过滤不回收会让列表与计数各说一套。

        并发（第 3 轮返工）：本方法可能被 ``asyncio.to_thread`` 放到工作线程执行，
        而事件循环线程上的挂起/裁决/回捞仍在变异 ``_requests``——直接迭代活字典会抛
        ``RuntimeError: dictionary changed size during iteration``。故**锁内只做一次
        浅拷贝快照**；``to_public_dict()`` 投影、并库面、known 集合构造全部放锁外，
        锁内绝不跨 DB I/O（否则只是把事件循环阻塞换成持锁线程等库、循环线程等锁）。
        """
        with self._lock:
            # 唯一一次内存面迭代，发生在锁下；快照之后与 _requests 再无关系
            snapshot = list(self._requests.values())
        items = [req.to_public_dict() for req in snapshot]
        if self._pending_store is not None:
            try:
                # 库行 status 是待审面的判据：只剔除「库里明确存在且非 pending」的项。
                # 两次库调用（statuses_for + list_unresolved）同在一个 try 里——它们
                # 共同构成"库面"，拆成两个降级点只会让读到一半时列表自相矛盾。
                statuses = self._pending_store.statuses_for([it["request_id"] for it in items])
                closed = {
                    it["request_id"]
                    for it in items
                    if statuses.get(it["request_id"], "pending") != "pending"
                }
                if closed:
                    # 过滤之外还要**回收**：待审面是本方法投影出来的，而 ``stats()``
                    # 的 ``pending`` 读的是 ``len(_requests)``——只过滤不回收会让两处
                    # 读数各说一套（列表空、计数 1）。本方法往往就是第一个得知"对端
                    # 已闭合"的地方，顺手收口比让残留无限期挂着更诚实。
                    with self._lock:
                        for sid in closed:
                            self._map.pop(sid, None)
                            self._requests.pop(sid, None)
                    items = [it for it in items if it["request_id"] not in closed]
                # known 基于**过滤后**的 items 构造（库行已闭合的项不会从
                # list_unresolved 回来，故不会重复列出；等价于旧的 set(self._requests)）
                known = {it["request_id"] for it in items}
                for row in self._pending_store.list_unresolved():
                    pub = row.get("sidecar") or {}
                    sid = pub.get("request_id")
                    if sid and sid not in known:
                        items.append(pub)
                        known.add(sid)
            except Exception as exc:  # noqa: BLE001 - 降级去向：内存面照常返回，读库故障以 error 日志可观察
                logger.error("HITL pending store read failed for listing: {}", exc)
        return items

    def stats(self) -> Dict[str, Any]:
        """审批观测面：拦截数 + 裁决分布 + 拒批占比。

        ``rejection_rate = rejected / decided``（decided 含 auto/bypass）。
        这是「误拒率」的可观测分子分母——真·误拒率需人工复核标注，
        库内不伪造。

        T19：``rejected`` **含**超时拒（到期自动拒绝也是拒绝）；``auto_reject_timeout``
        是其中按归因拆出的**子集**，不进 ``decided`` 求和（否则同一次裁决数两遍）；
        ``rejection_rate = rejected/decided`` 因此不因超时拒被稀释。该键只在
        ``deadline>0`` 时存在（约定 1：deadline=0 的键集合与基线逐字相同）。
        """
        decided = sum(
            self._counts[k]
            for k in (
                "approved",
                "rejected",
                "edited",
                "resumed",
                "auto",
                "bypass",
            )
            if k in self._counts
        )
        rejected = self._counts["rejected"]
        return {
            "enabled": bool(self._gate.enabled),
            "mode": self._mode.value,
            **self._counts,
            "pending": len(self._requests),
            "decided": decided,
            "rejection_rate": round(rejected / decided, 4) if decided else 0.0,
        }

    @property
    def gate(self) -> HumanReviewGate:
        return self._gate

    @property
    def sidecar(self) -> ReviewerSidecar:
        return self._sidecar

    @property
    def pending_store(self) -> Optional[Any]:
        """T18 库面（``HitlPendingStore`` 鸭子型）；None = 纯内存基线。"""
        return self._pending_store

    @property
    def sweep_owner(self) -> str:
        """本桥的扫描者/认领者标识（``host:pid:uuid8``）。"""
        return self._sweep_owner

    def sidecar_id_for_gate(self, gate_request_id: str) -> Optional[str]:
        """闸门请求 id → sidecar 请求 id（**裁决 API 的入参是后者**）。

        T23：流内审批帧要告诉前端"这条 pending 该往哪儿裁决"，而
        ``POST /ops/hitl/approvals/{sidecar_request_id}`` 收的是桥自己的 id。
        这里只是把桥内既有的 ``sidecar → gate`` 映射反过来读一次，**不新建映射**
        （约定 6 ④：同一事实不许两个源）。查不到返回 ``None``——调用方据此
        决定不写该字段，而不是编一个假 id 让裁决打到空气里。
        """
        if not gate_request_id:
            return None
        with self._lock:
            # 锁内只做一次内存遍历（绝不跨 DB I/O，锁语义见 __init__ 注释）
            for sidecar_id, mapped_gate_id in self._map.items():
                if mapped_gate_id == gate_request_id:
                    return sidecar_id
        return None

    def release_sweep_lease(self) -> bool:
        """让出扫描租约（关停路径调用）；无库面 ⇒ ``False``。

        进程走了还攥着租约，另一个 worker 要等到 TTL 到期才接手——这段时间里
        没人扫到期 pending，超时语义静默失效。故关停必须显式释放。
        异常**不在这里吞**：调用方（``main._shutdown``）负责留痕，关停清理
        的失败形态只该有一个口径。
        """
        if self._pending_store is None:
            return False
        return bool(self._pending_store.release_sweep_lease(self._sweep_owner))

    # -- 持久化（重启恢复） -------------------------------------------------

    def to_persistence(self) -> Dict[str, Any]:
        """序列化：Sidecar pending/结果 + id 映射 + 闸门请求记录。"""
        gate_requests: List[Dict[str, Any]] = []
        for gid in self._seen_gate_ids:
            req = self._gate.get(gid)
            if req is not None:
                gate_requests.append(req.to_dict())
        return {
            "sidecar": self._sidecar.to_persistence(),
            "mapping": dict(self._map),
            "gate_requests": gate_requests,
            "permission_mode": self._mode.value,
        }

    @classmethod
    def from_persistence(
        cls,
        data: Dict[str, Any],
        *,
        gate: Optional[HumanReviewGate] = None,
        breakpoints: Optional[Any] = None,
    ) -> "HitlBridge":
        """恢复（Sidecar pending + 闸门记录 + 映射）；``gate`` 通常传新实例。"""
        payload = dict(data or {})
        mode_raw = payload.get("permission_mode")
        mode = PermissionMode(mode_raw) if mode_raw else PermissionMode.MANUAL
        bridge = cls(
            gate=gate,
            sidecar=ReviewerSidecar.from_persistence(dict(payload.get("sidecar") or {})),
            breakpoints=breakpoints,
            permission_mode=mode,
        )
        sidecar_payload = dict(payload.get("sidecar") or {})
        # 实例尚未对外发布，但写点仍走同一把锁——"全部变异一个口径"只留一种做法，
        # 不留"这里可以不锁"的第二判据（将来调用形态变化时不会漏）
        with bridge._lock:
            for sid, pub in (sidecar_payload.get("pending") or {}).items():
                bridge._requests[sid] = SidecarReviewRequest(
                    request_id=pub["request_id"],
                    tool_name=pub["tool_name"],
                    arguments=dict(pub.get("arguments") or {}),
                    declared_risk_level=pub.get("declared_risk_level", "L0"),
                    evidence_ids=tuple(pub.get("evidence_ids") or ()),
                    artifact_refs=tuple(pub.get("artifact_refs") or ()),
                    created_at=pub.get("created_at", time.time()),
                )
        bridge._map = dict(payload.get("mapping") or {})
        for gd in payload.get("gate_requests") or []:
            try:
                req = _request_from_dict(gd)
            except Exception as exc:  # noqa: BLE001 - 坏记录跳过，不阻断恢复
                logger.warning("skip broken persisted gate request: {}", exc)
                continue
            if req.request_id not in bridge._seen_gate_ids:
                bridge._seen_gate_ids.append(req.request_id)
            if bridge._gate.get(req.request_id) is None:
                bridge._gate.restore_request(req)
        return bridge

    # -- T18 库面（真源回捞：启动 restore + 跨实例 decide） -------------------

    def restore_pending(self) -> int:
        """启动期回捞：把库中未决 pending 拉回内存（可裁决）。返回拉回条数。

        读库失败 ⇒ ``restore_errors`` 计数 + error 日志后**上抛**——调用方
        （``install_executor_hook``）决定不阻断接线，但绝不静默当"没有待审"
        （验收⑤/②）。坏行逐条跳过（``from_persistence`` 同式），一条坏行不
        该带走其余恢复。
        """
        if self._pending_store is None:
            return 0
        try:
            rows = self._pending_store.list_unresolved()
        except Exception as exc:  # noqa: BLE001 - 显式失败上抛（已计数+error 日志），降级去向由接线层定
            self._counts["restore_errors"] += 1
            logger.error("HITL pending restore failed (store read): {}", exc)
            raise
        restored = 0
        for row in rows:
            try:
                if self._apply_loaded_pending(row):
                    restored += 1
            except Exception as exc:  # noqa: BLE001 - 坏行跳过不阻断其余恢复（from_persistence 同式）
                logger.warning("skip broken persisted HITL pending row: {}", exc)
        if restored:
            logger.info("HITL pending restore pulled {} unresolved request(s) back", restored)
        return restored

    def _apply_loaded_pending(self, row: Optional[Dict[str, Any]]) -> bool:
        """把一条库记录装配进内存三面（``_map``/``_requests``/闸门记录）。

        只装配**未决**行：已决行不得复活再判一次（fail-closed 语义不因落库
        而放宽——同一 pending 只允许一次裁决，重复裁决维持 not_found）。
        返回是否装配（False = 不存在/已决/已在内存）。
        """
        if not row or row.get("status") != "pending":
            return False
        pub = dict(row.get("sidecar") or {})
        sid = str(pub.get("request_id") or "")
        if not sid or sid in self._requests:
            return False
        side_req = SidecarReviewRequest(
            request_id=sid,
            tool_name=pub.get("tool_name", ""),
            arguments=dict(pub.get("arguments") or {}),
            declared_risk_level=pub.get("declared_risk_level", "L0"),
            evidence_ids=tuple(pub.get("evidence_ids") or ()),
            artifact_refs=tuple(pub.get("artifact_refs") or ()),
            created_at=float(pub.get("created_at") or time.time()),
        )
        gate_id = str(row.get("gate_request_id") or "")
        # 回捞写入与挂起/裁决写入同锁：restore 与 decide 都可能在循环线程跑，
        # 而 pending() 可能正持快照在工作线程读
        with self._lock:
            self._map[sid] = gate_id
            self._requests[sid] = side_req
        self._sidecar.restore(side_req)
        if gate_id and gate_id not in self._seen_gate_ids:
            self._seen_gate_ids.append(gate_id)
        gd = row.get("gate_request")
        if gd:
            # 闸门记录按原载荷同式重建（scope/等级保持挂起时判定，不在回捞处重算）
            try:
                req = _request_from_dict(gd)
                if self._gate.get(req.request_id) is None:
                    self._gate.restore_request(req)
            except Exception as exc:  # noqa: BLE001 - 坏闸门记录：decide 侧还有 _rebuild_gate_request 兜底
                logger.warning("bad persisted gate record for {}: {}", sid, exc)
        return True

    # -- T19 审批时限（Q9 裁决：到期自动拒绝） -----------------------------

    #: 闸门终态 → 库闭合桶（对账补偿用；只覆盖"闸门已决"的两种可能终态）
    _BUCKET_BY_GATE_DECISION: Dict[Any, str] = {
        ReviewDecision.APPROVED: "approved",
        ReviewDecision.REJECTED: "rejected",
    }

    async def expire_overdue(
        self,
        deadline_s: float,
        *,
        now: Optional[float] = None,
        limit: int = 100,
    ) -> List[BridgeDecision]:
        """把超过审批时限仍未决的 pending 逐条**自动拒绝**（fail-closed）。

        语义分层（``docs/architecture/adr/0007-执行沙箱端口.md`` §7）：到期拒是**治理层终局**——
        闸门落 ``reject`` 后执行点拿到的是"没批准"，不重开执行、不重试、不还原图
        （挂起车道本来就没让图停住：hook 2 秒超时后 executor 已返回 blocked）。

        **必须复用 :meth:`decide`**：拒绝路径只有一条（闸门 reject + sidecar 收口 +
        清内存 + 指标 + 库闭合 + 断点解除）。在超时扫描里另写一遍，等于把"什么算
        一次拒绝"变成两个源（约定 6 ④：改一处漏一处）。

        **并发模型（T19 第 2 轮返工；多 worker 部署 ``--workers 2`` 是默认形态）**：

        ① **扫描租约**保证同一时刻只有一个扫描者：每个 worker 起的扫描任务都先
           ``acquire_sweep_lease``，租约带 TTL（120 秒），持有者崩溃后由其他 worker
           到期接管。拿不到租约的那一轮**不读库、不拒绝**，pending 留给持有者。
        ② **行认领**是正确性兜底：租约只降低"两个扫描者"的概率，不消除它（抢占、
           到期竞争、跨机时钟都还在）。逐行 ``claim_expired`` 是原子条件更新，
           因此**即使两个扫描者同时开扫，同一行也只被一个裁决**。
        ③ 库闭合失败时**保留认领**，租约/认领 TTL 到期后由 :meth:`_reconcile_stale_pending`
           按**闸门里的既有裁决**补偿闭合库行——绝不下一轮立即重算（那会把同一次
           裁决再数一遍：``rejected`` 与 ``auto_reject_timeout`` 各 +1、超时指标再打一次）。
           认领 TTL 也是 120 秒 ⇒ close 失败后这一行最坏多等一个认领 TTL 才被对账。
        ④ **闸门记录被逐出时按 sidecar 载荷补建后再拒**（T19 第 4 轮返工）：
           ``HumanReviewGate`` 的 FIFO 上限（200）对最老记录不区分是否 pending 直接
           逐出，于是"``record is None``"有两种含义——已决、或者**被逐出**。把后者当
           已决会让 `gate.reject` 恒返回 False，这行永久停在 pending：每轮空转
           （``did_not_land`` + 释放认领）、永久占一个 ``max_unresolved`` 槽位，还因为
           按 ``created_at`` 升序取数而挡住后面的到期行。故按 :meth:`decide` 批准分支
           同一做法补建闸门记录，再走同一条 :meth:`decide`。

        失败姿态（约定 2 fail-open，与既有读路径同一取舍）：
        - 租约不可得（读/写库异常）⇒ ``logger.warning`` + 返回 ``[]``——fail-open
          的正确方向是"这轮不拒"（pending 保留待下轮），而不是"没有租约也照拒"；
        - 读库失败 ⇒ ``logger.warning`` + 返回 ``[]``（已处理部分保留，主链不受影响）；
        - 单行失败（坏载荷 / 闸门侧异常）⇒ ``logger.warning`` 后继续处理其余行。

        计数口径（T19 返工，与 ``retrieval/usecases/approval_service.py`` 同形）：
        ``decide`` 已把这次裁决计进 ``rejected`` 并打 ``decision="rejected"``；本方法
        经 ``attribution`` 再给 ``auto_reject_timeout`` 这个**子集**计数 +1，并补打
        ``record_approval_timeout``。超时拒因此仍是 ``rejected`` 的一种（不与人工拒
        互斥分桶），``decided`` 也不会把一次裁决数两遍。

        ``deadline_s <= 0`` 或没有库面 ⇒ ``[]``（扫描链一次库都不读）。
        """
        deadline = float(deadline_s or 0.0)
        if deadline <= 0 or self._pending_store is None:
            return []
        cutoff = (now if now is not None else time.time()) - deadline

        # 第 1 道并发闸：扫描租约。拿不到就本轮什么都不做（见 docstring ①）。
        # 异常走 warning（而不是 debug）：租约面读不到库属配置/连通性故障，
        # 静默会让"扫描器一直在跳过"完全不可观察。
        try:
            leased = await asyncio.to_thread(
                self._pending_store.acquire_sweep_lease, self._sweep_owner
            )
        except Exception as exc:  # noqa: BLE001 - 降级去向：本轮不拒绝任何一条（pending 保留），warning 留痕待下一轮
            logger.warning("HITL deadline sweep lease unavailable (skipping this round): {}", exc)
            return []
        if not leased:
            # 没抢到租约不是失败：另一个 worker 正在扫，安静让路（debug 级）。
            # 注意这里打的是**本人**的 owner（想说明"是这台没抢到"），持有者是谁不
            # 在本进程可知——排障请看 ``hitl_sweep_leases.owner`` 那一行。
            logger.debug(
                "HITL deadline sweep skipped this round: lease held by another sweeper "
                "(this sweeper owner={}; current holder: see hitl_sweep_leases.owner)",
                self._sweep_owner,
            )
            return []

        try:
            # 同步 repo 经 to_thread 下放，不卡事件循环（约定 6 ②）
            rows = await asyncio.to_thread(
                self._pending_store.list_expired, cutoff, limit=limit
            )
        except Exception as exc:  # noqa: BLE001 - 降级去向：本轮不拒绝任何一条（保留待审），warning 留痕待下一轮
            logger.warning(
                "HITL approval deadline sweep could not read expired pendings "
                "(nothing rejected this round, will retry): {}",
                exc,
            )
            return []

        decisions: List[BridgeDecision] = []
        for row in rows:
            sid = str(
                (row.get("sidecar") or {}).get("request_id")
                or row.get("sidecar_request_id")
                or ""
            )
            # 第 2 道并发闸：行认领。**先判 sid 再认领**——条件更新以主键为锚，
            # 没有 id 的行连"这一行是谁"都指不出来。
            try:
                if not sid:
                    raise ValueError("expired pending row carries no sidecar request id")
                claimed = await asyncio.to_thread(
                    self._pending_store.claim_expired,
                    sid,
                    cutoff_epoch=cutoff,
                    owner=self._sweep_owner,
                )
            except Exception as exc:  # noqa: BLE001 - 降级去向：跳过这一行、其余到期行照常拒绝，warning 可观察
                logger.warning(
                    "HITL approval deadline reject failed for {} (other expires continue): {}",
                    sid or "?",
                    exc,
                )
                continue
            if not claimed:
                # 已决 / 未到期 / 已被他人认领 / 自己的认领仍在 TTL 内（close 失败
                # 的残留）——都不是失败，安静跳过；重试窗口由 TTL 决定。
                logger.debug(
                    "HITL expired pending {} is not claimable by this sweeper (skipped)", sid
                )
                continue
            try:
                # 库行 → 内存三面（T18 既有装配口径，不在这里另造一份）
                self._apply_loaded_pending(row)

                # 认领后的**对账前置检查**：闸门已决 + 库行仍 pending，说明上一次
                # "裁决落地、库闭合失败"（decide 的 persist_error 路径）留了残留。
                # 这里重算 = 同一次裁决数两遍，唯一正确的动作是按闸门里那条既有
                # 裁决把库行补闭合（不重新计数、不打超时指标）。
                gate_id = self._map.get(sid)
                record = self._gate.get(gate_id) if gate_id else None
                if record is None and gate_id:
                    # 闸门记录可能"**没了**"而不是"已决"：``HumanReviewGate._register``
                    # 的 FIFO 上限（``max_requests=200``）对最老记录**不区分是否
                    # pending** 直接逐出。逐出后 gate.reject 因"记录不存在"返回 False
                    # ⇒ 这一行永远拒不掉（每轮 did_not_land + release_claim，永久占
                    # max_unresolved 槽位，还挡着 list_expired 后面的行）。按
                    # ``decide`` 批准分支同一做法补建后仍走同一条 decide——不新造
                    # 第二条重建路径，这样"什么算一次拒绝"仍只有一个源。
                    request = self._requests.get(sid)
                    if request is not None:
                        rebuilt = self._rebuild_gate_request(gate_id, request, approved=False)
                        record = (
                            rebuilt
                            if self._gate.restore_request(rebuilt)
                            else self._gate.get(gate_id)
                        )
                if record is not None and record.decision is not ReviewDecision.PENDING:
                    await self._reconcile_stale_pending(sid, record)
                    continue
                # 走到这里 record 仍可能是 None：``gate_id`` 为空（坏行）或 ``_requests``
                # 没有该 sid ⇒ 没有 sidecar 载荷可依，补建不出闸门记录。这是坏数据，
                # 保持原行为（decide 落 not_found / did_not_land + warning），需运维修。
                decision = await self.decide(
                    sid,
                    ReviewDecisionAction.REJECT,
                    reason=AUTO_REJECT_TIMEOUT_REASON,
                    by=AUTO_REJECT_TIMEOUT_ACTOR,
                    attribution=AUTO_REJECT_TIMEOUT_REASON,
                    # 闸门侧没新落地就不许计数/闭合（见 decide docstring）
                    require_gate_pending=True,
                )
            except Exception as exc:  # noqa: BLE001 - 降级去向：跳过这一行、其余到期行照常拒绝，warning 可观察
                # 裁决没跑完 ⇒ 让出认领，下一轮还能重试（否则要白等一个 TTL）。
                await self._release_claim(sid, why="reject raised")
                logger.warning(
                    "HITL approval deadline reject failed for {} (other expires continue): {}",
                    sid,
                    exc,
                )
                continue
            if decision.not_found or decision.error:
                # "库里到期但裁决没落下"必须显式可见：这不是成功，不得计数。
                # 让出认领让下一轮重试（``gate_request_not_pending`` 属竞态下没落地，
                # 同样不该把认领占着）。
                await self._release_claim(
                    sid,
                    why=f"decision did not land (not_found={decision.not_found}, "
                    f"error={decision.error})",
                )
                logger.warning(
                    "HITL approval deadline reject did not land for {} (not_found={}, error={})",
                    sid,
                    decision.not_found,
                    decision.error,
                )
                continue
            if decision.persist_error:
                # 闸门已拒、库未闭合。这次拒绝在闸门侧**确实落地了**，所以照常计数
                # 与打点；但认领**保留**（不释放）：下一轮若立刻重算，就是同一次
                # 裁决第二次落地。TTL 到期后由对账路径按闸门既有裁决补闭合。
                logger.error(
                    "HITL approval deadline reject landed in gate but pending row was NOT "
                    "closed for {} (claim kept until TTL expiry, then reconciled against the "
                    "existing gate decision): {}",
                    sid,
                    decision.persist_error,
                )
            # 指标对（与仓里既有约定同形：先一条裁决指标，超时再补一条超时指标，
            # 见 ``retrieval/usecases/approval_service.py``）。裁决指标已在 decide 内
            # 打过 ``decision="rejected"``——六值封闭集不动，不给超时另造 decision 值；
            # 这里补的是"这次拒绝属超时归因"。``target_type="tool"`` 与挂起/桥路径
            # 逐字一致（已在 p100 白名单内，零新增 label）。打点故障不倒灌审批链
            # （约定 2，与 ``_record_decision`` 同取舍）。
            try:
                record_approval_timeout("tool")
            except Exception as exc:  # noqa: BLE001 - 指标故障不影响审批链
                logger.debug("record_approval_timeout failed: {}", exc)
            decisions.append(decision)

        if decisions:
            logger.info(
                "HITL approval deadline swept {} overdue pending(s) into rejected "
                "(deadline={}s, by={})",
                len(decisions),
                deadline,
                AUTO_REJECT_TIMEOUT_ACTOR,
            )
        return decisions

    async def _reconcile_stale_pending(self, sidecar_request_id: str, record: Any) -> None:
        """闸门已决但库行仍 pending：用既有裁决闭合库行（补偿），不重新计数、不打超时指标。

        这条路径的存在理由就是"绝不重复计数"：闸门那条记录已经是终局（谁、何时、
        为什么都已写在上面），库行只是没跟上。再拒一次会让 ``rejected`` /
        ``auto_reject_timeout`` 各多 1、超时指标多打一次，而治理面上什么都没变。
        映射不到桶（理论不可达的闸门终态）⇒ 不猜，保留认领留待运维。

        **补偿只做库闭合**（第 4 轮返工显式声明，免得后来者"顺手补全"）：刻意不重放
        :meth:`_resolve_interrupt`、不传 ``edited_arguments``、不动 sidecar 待审面——
        原裁决在 :meth:`decide` 里已经做过这些，重放会把已终局的断点面再动一次
        （重复 ``resolve_interrupt`` / 覆盖已写的 override 数据）。前提是**同一实例、
        原裁决已完整走完 decide**：``persist_error`` 只由 close 那一步抛错产生，而那一步
        位于「闸门裁决 + 内存清理 + 计数与指标」之后，且 close 抛错**不会中断** decide
        后续的状态转移与断点解除（那两段在 close 之后无条件执行）⇒ 走到补偿路径时，
        除了库行之外该做的都已做过。跨实例场景下断点面由**执行裁决的那个实例**负责
        （本轮返工让后手直接 ``not_found``，不重复触碰）。
        """
        bucket = self._BUCKET_BY_GATE_DECISION.get(record.decision)
        if bucket is None:
            logger.warning(
                "HITL stale pending {} carries unexpected gate decision {} "
                "(claim kept, not reconciled)",
                sidecar_request_id,
                record.decision,
            )
            return
        try:
            closed = await asyncio.to_thread(
                self._pending_store.close_decision,
                sidecar_request_id,
                status=bucket,
                decided_by=record.decided_by or "",
                reason=record.note or "",
            )
        except Exception as exc:  # noqa: BLE001 - 降级去向：保留认领，认领 TTL 到期后重试对账
            logger.warning(
                "HITL stale pending {} could not be reconciled yet "
                "(claim kept, will retry after claim TTL): {}",
                sidecar_request_id,
                exc,
            )
            return
        if closed is False:
            # 对端实例在"先核库行 status、后写闭合"的窗口内闭合了同一行（裁决只写
            # 一次）。返回值已决不是异常，但本实例**没写进去**：不能声称已对账，
            # 也不能 pop（内存挂起面仍须与库面一致地由下次裁决/收敛处理）；保留
            # 认领（不 release），TTL 到期后重新走对账即可，期间不重复计数。
            logger.warning(
                "HITL stale pending {} row was already closed by a peer instance; this "
                "instance's reconcile was NOT written (not re-counted, claim kept)",
                sidecar_request_id,
            )
            return
        # 闭合成功 ⇒ 内存挂起面随之一并收口（与 decide 同源同锁）。
        with self._lock:
            self._map.pop(sidecar_request_id, None)
            self._requests.pop(sidecar_request_id, None)
        logger.warning(
            "HITL approval deadline reconciled stale pending {} with earlier gate "
            "decision {} (not re-counted)",
            sidecar_request_id,
            bucket,
        )

    async def _release_claim(self, sidecar_request_id: str, *, why: str) -> None:
        """裁决未落地时让出认领；释放失败只留痕（认领到期自愈）。"""
        try:
            await asyncio.to_thread(
                self._pending_store.release_claim,
                sidecar_request_id,
                owner=self._sweep_owner,
            )
        except Exception as exc:  # noqa: BLE001 - 降级去向：认领 TTL 到期自愈，下一轮仍可被扫到
            logger.warning(
                "HITL approval deadline claim release failed for {} ({}; "
                "self-heals after claim TTL): {}",
                sidecar_request_id,
                why,
                exc,
            )

    # -- internals ---------------------------------------------------------

    def _accept_edits_auto(self, request: ReviewRequest, args: Dict[str, Any]) -> bool:
        """``accept_edits`` 的自动放行判定：编辑类名 + 无提权 + 无声明级参与。

        三者缺一即回落手工挂起——声明 ``L2``（需人批）或 ``force`` 之类的
        参数提权**绝不**因模式而被自动放行。
        """
        tool = request.tool_name
        if not _EDIT_NAME_RE.search(tool):
            return False
        base = classify_risk(tool, None, policy=self._gate.policy)
        with_args = classify_risk(tool, args, policy=self._gate.policy)
        if rank_of(with_args) > rank_of(base):
            return False  # 参数提权参与了拦截
        if rank_of(request.risk_level) > rank_of(with_args):
            return False  # 声明级参与了拦截（声明需人批）
        return True

    def _effective_level_for(self, tool: str, args: Dict[str, Any]) -> RiskLevel:
        """按执行点 preflight 同式计算有效风险级（含声明级）。"""
        declared_str: Optional[str] = None
        if tool_registry.has_tool(tool):
            declared_str = tool_registry.get_spec(tool).risk_level
        return effective_risk_level(
            tool, args, declared_risk_level=declared_str, policy=self._gate.policy
        )

    def _rebuild_gate_request(
        self, gate_id: str, side_req: SidecarReviewRequest, *, approved: bool
    ) -> ReviewRequest:
        """闸门记录丢失时按 Sidecar 载荷重建（scope 与 preflight 同式）。"""
        return ReviewRequest(
            request_id=gate_id,
            tool_name=side_req.tool_name,
            risk_level=self._effective_level_for(side_req.tool_name, side_req.arguments),
            reason="recovered from sidecar (HITL bridge)",
            args_digest=digest_args(side_req.arguments),
            decision=ReviewDecision.APPROVED if approved else ReviewDecision.PENDING,
            decided_at=time.time() if approved else None,
            decided_by="hitl-bridge-recover",
        )

    def _transition(self, state: Optional[AgentState], target: AgentStatus) -> None:
        """状态机转移（失败仅告警——审批挂起不因状态簿记而失败）。"""
        if state is None:
            return
        try:
            LifecycleManager.transition(state, target)
        except Exception as exc:  # noqa: BLE001 - 非法转移不阻断审批链
            logger.warning(
                "HITL bridge status transition {} -> {} skipped: {}",
                state.status.value,
                target.value,
                exc,
            )

    async def _record_interrupt(
        self, interrupt_id: str, thread_id: str, node_name: str, request: ReviewRequest
    ) -> None:
        """栈C：挂起时登记 HUMAN_APPROVAL 断点（best-effort，故障自吞）。"""
        if self._breakpoints is None:
            return
        try:
            from agentix.application.services.breakpoint_resume import (
                InterruptReason,
                InterruptRecord,
            )

            record = InterruptRecord(
                interrupt_id=interrupt_id,
                thread_id=thread_id or "-",
                node_name=node_name,
                reason=InterruptReason.HUMAN_APPROVAL,
                state_snapshot={
                    "gate_request_id": request.request_id,
                    "tool_name": request.tool_name,
                    "risk_level": request.risk_level.value,
                },
            )
            await self._breakpoints.record_interrupt(record)
        except Exception as exc:  # noqa: BLE001 - Redis/断点面故障不影响挂起
            logger.warning("HITL bridge record_interrupt failed (best-effort): {}", exc)

    async def _resolve_interrupt(
        self, interrupt_id: str, action: ReviewDecisionAction, merged: Optional[Dict[str, Any]]
    ) -> None:
        """栈C：裁决落地后解除断点（best-effort，故障自吞）。"""
        if self._breakpoints is None:
            return
        try:
            from agentix.application.services.breakpoint_resume import ResumeAction

            resume = {
                ReviewDecisionAction.APPROVE: ResumeAction.CONTINUE,
                ReviewDecisionAction.RESUME: ResumeAction.CONTINUE,
                ReviewDecisionAction.EDIT: ResumeAction.OVERRIDE,
                ReviewDecisionAction.REJECT: ResumeAction.ABORT,
            }[action]
            override = {"arguments": merged} if merged is not None else None
            await self._breakpoints.resolve_interrupt(
                interrupt_id, resume, override_data=override
            )
        except Exception as exc:  # noqa: BLE001 - Redis/断点面故障不影响裁决
            logger.warning("HITL bridge resolve_interrupt failed (best-effort): {}", exc)

    def _record_decision(self, label: str) -> None:
        try:
            record_approval_decision(target_type="tool", decision=label)
        except Exception as exc:  # noqa: BLE001 - 指标故障不影响审批链
            logger.debug("record_approval_decision failed: {}", exc)


def _request_from_dict(data: Dict[str, Any]) -> ReviewRequest:
    """从 ``ReviewRequest.to_dict()`` 重建（重启恢复通道）。"""
    return ReviewRequest(
        request_id=data["request_id"],
        tool_name=data["tool_name"],
        risk_level=RiskLevel(data["risk_level"]),
        reason=data.get("reason", ""),
        args_digest=dict(data.get("args_digest") or {}),
        trace_id=data.get("trace_id", "-"),
        created_at=float(data.get("created_at") or time.time()),
        decision=ReviewDecision(data.get("decision") or ReviewDecision.PENDING.value),
        decided_at=data.get("decided_at"),
        decided_by=data.get("decided_by"),
        note=data.get("note", ""),
        consumed=bool(data.get("consumed", False)),
    )


# ---------------------------------------------------------------------------
# 单例 + 生产接线
# ---------------------------------------------------------------------------

_bridge: Optional[HitlBridge] = None


def _resolve_pending_store() -> Optional[Any]:
    """T18 装配判定**唯一源**：flag-off（默认）恒 ``None``。

    flag-off 时连 store 模块都不 import——不触 sqlalchemy、不建池、不建表、
    不读写新表（约定 1 的"逐位等价"要求装配点本身零新副作用，而不是"建了但不用"）。
    flag 判据只在这里读一次；``get_hitl_bridge`` 与测试 monkeypatch 共用本函数，
    不许第二个判点（约定 6 ④）。
    """
    from agentix.config import settings

    if not bool(getattr(settings, "HITL_PENDING_PERSIST_ENABLED", False)):
        return None
    from agentix.infrastructure.persistence.hitl_pending_store import (
        build_default_hitl_pending_store,
    )

    return build_default_hitl_pending_store()


def resolve_pending_store() -> Optional[Any]:
    """公开入口：只委托 :func:`_resolve_pending_store`，判据仍在那里、不另读 flag。"""
    return _resolve_pending_store()


def resolve_approval_deadline_s() -> float:
    """T19 配置读取**唯一源**：``HITL_APPROVAL_DEADLINE_S``（0 = 关 = 现行为）。

    settings 中 ``HITL_APPROVAL_DEADLINE_S`` 的**唯一读取函数**；
    :func:`get_hitl_bridge` 与 ``main._start_hitl_deadline_sweeper`` 都调它。
    与 :func:`_resolve_pending_store` 同理：设置在启动期被读一次、装配进单例，
    桥内不再二次读 ``settings``（测试注入只需换构造参数一处）。
    """
    from agentix.config import settings

    return float(getattr(settings, "HITL_APPROVAL_DEADLINE_S", 0.0) or 0.0)


def get_hitl_bridge() -> HitlBridge:
    """全局桥单例（``gate`` 缺省 = 全局闸门，由 settings 决定开关）。

    T18：pending 库面按 :func:`_resolve_pending_store` 装配——flag-off 得到
    ``None``，与基线构造逐位等价。
    T19：审批时限按 :func:`resolve_approval_deadline_s` 装配——默认 0 时
    ``stats()`` 连 ``auto_reject_timeout`` 键都不会出现（约定 1）。
    """
    global _bridge
    if _bridge is None:
        _bridge = HitlBridge(
            pending_store=_resolve_pending_store(),
            approval_deadline_s=resolve_approval_deadline_s(),
        )
    return _bridge


def reset_hitl_bridge() -> None:
    global _bridge
    _bridge = None
    # 决策面标记随桥一并归零（A2a）：桥拆了就没有决策通道，下次开闸
    # 应当重新给出「无人可批」告警。
    from agentix.tools.gateway.human_review import clear_decision_surface

    clear_decision_surface()


def install_executor_hook() -> HitlBridge:
    """把全局桥挂到全局 ``ToolExecutor`` 的拦截缝（幂等）。

    **仅在 ``settings.HUMAN_REVIEW_ENABLED=true`` 时由 main 启动期调用**；
    flag 关（默认）不调用 ⇒ executor 保持 ``hook=None`` 的零变化路径。

    A2a：接线前先 ``mark_decision_surface()``——本桥的 ``bridge.decide``
    就是审批决策面，先宣告再创建全局闸门，避免「开了却无人可批」的
    误导性启动告警（告警只保留给真正的死配置）。
    """
    from agentix.tools.gateway.executor import get_tool_executor
    from agentix.tools.gateway.human_review import mark_decision_surface

    mark_decision_surface()
    bridge = get_hitl_bridge()
    get_tool_executor().set_review_blocked_hook(bridge.open_from_gate)
    if bridge.pending_store is not None:
        # T18 验收② 启动期 restore：未决 pending 拉回内存才可裁决。
        # restore_pending 内已做 error 日志 + restore_errors 计数；这里捕获只保证
        # **不阻断接线**——闸门侧照旧 fail-closed，回捞失败绝不静默当"没有待审"
        # （可见性由计数与日志承担，不是靠打断启动）。
        try:
            bridge.restore_pending()
        except Exception:  # noqa: BLE001 - 降级去向：已在 restore_pending 内计数+error 留痕，这里只防启动被打断
            pass
    return bridge
