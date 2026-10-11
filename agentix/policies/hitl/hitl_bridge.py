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

import re
import time
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
)
from agentix.runtime.lifecycle import LifecycleManager
from agentix.runtime.state import AgentState, AgentStatus

__all__ = [
    "PermissionMode",
    "BridgeSuspend",
    "BridgeDecision",
    "HitlBridge",
    "get_hitl_bridge",
    "reset_hitl_bridge",
    "install_executor_hook",
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
    ) -> None:
        self._gate: HumanReviewGate = gate if gate is not None else get_review_gate()
        self._sidecar: ReviewerSidecar = sidecar if sidecar is not None else ReviewerSidecar()
        #: BreakpointManager（需有 ``record_interrupt``/``resolve_interrupt``）；可为 None
        self._breakpoints = breakpoints
        self._mode = PermissionMode(permission_mode)
        #: sidecar_request_id → gate_request_id
        self._map: Dict[str, str] = {}
        #: sidecar_request_id → 桥自留的 Sidecar 请求副本（含原始 args）
        self._requests: Dict[str, SidecarReviewRequest] = {}
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
        self._map[side_req.request_id] = gate_id
        self._requests[side_req.request_id] = side_req
        self._transition(state, AgentStatus.WAITING_APPROVAL)
        await self._record_interrupt(side_req.request_id, thread_id, node_name, request)
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
    ) -> BridgeDecision:
        """执行四裁决并落回闸门。

        - ``APPROVE`` / ``RESUME`` → ``gate.approve``（下次执行即消费）；
        - ``EDIT`` → 合并 edits，按 preflight 同式重算审批记录的
          ``args_digest`` / ``risk_level``（编辑后 scope 精确等于人工所批），
          返回 ``merged_arguments`` 供执行点重试；
        - ``REJECT``（含 Sidecar approver 否决）→ ``gate.reject``（fail-closed）。
        """
        action = ReviewDecisionAction(action)
        gate_id = self._map.get(sidecar_request_id)
        request = self._requests.get(sidecar_request_id)
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
            self._gate.reject(gate_id, by=by, note=result.reason or "hitl-bridge:reject")

        # 裁决后：释放挂起（映射与副本不再需要；闸门记录留待执行点消费）
        self._map.pop(sidecar_request_id, None)
        self._requests.pop(sidecar_request_id, None)

        self._record_decision(result.action.value)
        bucket = {
            ReviewDecisionAction.APPROVE: "approved",
            ReviewDecisionAction.REJECT: "rejected",
            ReviewDecisionAction.EDIT: "edited",
            ReviewDecisionAction.RESUME: "resumed",
        }[result.action]
        self._counts[bucket] += 1

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
        )

    # -- 查询 / 观测 -------------------------------------------------------

    def pending(self) -> List[Dict[str, Any]]:
        """待审面投影（给审批 UI；**无** Agent trajectory / narrative）。"""
        return [req.to_public_dict() for req in self._requests.values()]

    def stats(self) -> Dict[str, Any]:
        """审批观测面：拦截数 + 裁决分布 + 拒批占比。

        ``rejection_rate = rejected / decided``（decided 含 auto/bypass）。
        这是「误拒率」的可观测分子分母——真·误拒率需人工复核标注，
        库内不伪造。
        """
        decided = sum(
            self._counts[k]
            for k in ("approved", "rejected", "edited", "resumed", "auto", "bypass")
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


def get_hitl_bridge() -> HitlBridge:
    """全局桥单例（``gate`` 缺省 = 全局闸门，由 settings 决定开关）。"""
    global _bridge
    if _bridge is None:
        _bridge = HitlBridge()
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
    return bridge
