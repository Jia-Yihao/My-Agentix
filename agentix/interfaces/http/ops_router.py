"""
Observability, breakpoint resume, and HITL approval API endpoints.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from agentix.application.services.observability import context_report, get_observability
from agentix.application.services.breakpoint_resume import (
    get_breakpoint_manager,
    get_checkpoint_recovery,
    ResumeAction,
)
from agentix.infrastructure.core.security import verify_api_key

#: 机器面：接线 verify_api_key（REQUIRE_API_KEY 开启时校验 x-api-key，中间件外兜底）。
router = APIRouter(prefix="/ops", tags=["operations"], dependencies=[Depends(verify_api_key)])


# ── Observability endpoints ──────────────────────────────────────────────


@router.get("/dashboard")
async def get_dashboard() -> Dict[str, Any]:
    """Get aggregated system metrics dashboard."""
    obs = get_observability()
    return await obs.get_dashboard()


@router.get("/llm-calls")
async def get_llm_calls(
    limit: int = Query(50, ge=1, le=500),
) -> List[Dict[str, Any]]:
    """Get recent LLM call records."""
    obs = get_observability()
    return await obs.get_recent_llm_calls(limit=limit)


@router.get("/requests")
async def get_requests(
    limit: int = Query(50, ge=1, le=500),
) -> List[Dict[str, Any]]:
    """Get recent request records."""
    obs = get_observability()
    return await obs.get_recent_requests(limit=limit)


@router.get("/context")
async def get_context_report() -> Dict[str, Any]:
    """P3（渐进上下文）：context subsystem report — read-only contract
    (no provider calls, no state mutation; see observability.context_report)."""
    return context_report()


@router.get("/events")
async def list_events(
    event_type: Optional[str] = Query(
        None, description="Filter by event type (e.g. user_notification)"
    ),
    limit: int = Query(50, ge=1, le=500),
) -> List[Dict[str, Any]]:
    """List recent EventPort events (Phase 23；鸭子面 list_recent，无则空表)."""
    import inspect as _inspect

    from agentix.agent.events.port import get_event_port  # 惰性：测试可 reset 单例

    port = get_event_port()
    lister = getattr(port, "list_recent", None)
    if lister is None:
        return []
    result = lister(event_type, limit=limit)
    if _inspect.isawaitable(result):
        result = await result  # redis_stream 总线为 async 扩展面
    return list(result)


# ── Breakpoint resume endpoints ──────────────────────────────────────────


class ResolveRequest(BaseModel):
    """Request body for resolving an interrupt."""
    interrupt_id: str = Field(..., description="Interrupt ID to resolve")
    action: str = Field(
        ...,
        description="Resume action: continue, retry, skip, abort, override",
    )
    override_data: Optional[Dict[str, Any]] = Field(
        None,
        description="Optional data to inject when using override action",
    )


class ResumeRequest(BaseModel):
    """Request body for resuming a crashed thread."""
    thread_id: str = Field(..., description="Thread ID to resume")


@router.get("/interrupts")
async def list_interrupts(
    thread_id: Optional[str] = Query(None, description="Filter by thread ID"),
    unresolved_only: bool = Query(True, description="Only show unresolved interrupts"),
) -> List[Dict[str, Any]]:
    """List interrupt records."""
    mgr = get_breakpoint_manager()

    if thread_id:
        records = await mgr.get_thread_interrupts(thread_id, unresolved_only=unresolved_only)
    else:
        records = []

    return [r.to_dict() for r in records]


@router.get("/interrupts/{interrupt_id}")
async def get_interrupt(interrupt_id: str) -> Dict[str, Any]:
    """Get a specific interrupt record."""
    mgr = get_breakpoint_manager()
    record = await mgr.get_interrupt(interrupt_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Interrupt not found: {interrupt_id}",
        )
    return record.to_dict()


@router.post("/interrupts/resolve")
async def resolve_interrupt(request: ResolveRequest) -> Dict[str, Any]:
    """Resolve an interrupt with a specific action."""
    try:
        action = ResumeAction(request.action)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid action: {request.action}. Must be one of: continue, retry, skip, abort, override",
        )

    mgr = get_breakpoint_manager()
    record = await mgr.resolve_interrupt(
        request.interrupt_id,
        action,
        override_data=request.override_data,
    )
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Interrupt not found: {request.interrupt_id}",
        )
    return record.to_dict()


@router.get("/active-threads")
async def list_active_threads() -> List[str]:
    """List threads with incomplete executions (potential crash victims)."""
    recovery = get_checkpoint_recovery()
    return await recovery.get_active_threads()


@router.post("/threads/resume")
async def resume_thread(request: ResumeRequest) -> Dict[str, Any]:
    """Attempt to resume a crashed thread from its last checkpoint."""
    recovery = get_checkpoint_recovery()
    metadata = await recovery.get_checkpoint_metadata(request.thread_id)
    if not metadata:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No checkpoint found for thread: {request.thread_id}",
        )

    # 说明（非缺陷）：此处为函数体内（惰性）import，保留原样。函数内 `from X import y`
    # 每次执行都会重新读取模块属性，总能取到当前值，语义等价于 `lg_builder.graph`，
    # 因此不受启动期 Redis checkpointer 覆盖的影响。请勿改为模块级 import——
    # 值绑定会在 import 时固化，反而可能拿到启动期覆盖之前的默认图。
    from agentix.orchestration.langgraph.lg_builder import graph

    result = await recovery.resume_thread(request.thread_id, graph)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to resume thread",
        )

    messages = result.get("messages", [])
    return {
        "thread_id": request.thread_id,
        "status": "resumed",
        "message_count": len(messages),
        "last_message": messages[-1].content if messages else "",
    }


# ── HITL approval endpoints（A4：生产审批决策面）────────────────────────


class InLoopResumeRequest(BaseModel):
    """F1e·A-P1-4 进环 HITL 恢复请求体。"""

    thread_id: str = Field(..., description="挂起线程（= session_id）")
    resume_value: str = Field(
        "approve", description="裁决值：approve（落批准+重试）| reject（fail-closed）"
    )


@router.post("/hitl/in-loop")
async def resume_in_loop_hitl(request: InLoopResumeRequest) -> Dict[str, Any]:
    """A-P1-4① 进环 HITL 恢复面：裁决值经 ``Command(resume=...)`` 回注图。

    langgraph 依赖在 ``lg_builder``（p66 白名单），本路由惰性调用其
    ``resume_in_loop`` 助手；裁决落账由 ``task_execution`` 消费侧负责
    （approve ⇒ gate.approve 后重试，reject ⇒ gate.reject + 快速失败）。
    非 approve|reject ⇒ 400；线程无挂起/未知 thread ⇒ 图侧抛错转 409。
    """
    if request.resume_value not in ("approve", "reject"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"resume_value must be approve|reject, got: {request.resume_value}",
        )

    from agentix.orchestration.langgraph.lg_builder import resume_in_loop

    try:
        result = await resume_in_loop(request.thread_id, request.resume_value)
    except Exception as exc:  # noqa: BLE001 - 无挂起/未知 thread 等图侧错误
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"in-loop resume failed: {exc}",
        )
    messages = result.get("messages", []) if isinstance(result, dict) else []
    return {
        "thread_id": request.thread_id,
        "status": "resumed",
        "resume_value": request.resume_value,
        "message_count": len(messages),
    }


class ApprovalDecisionRequest(BaseModel):
    """裁决请求体（四裁决词汇与 sidecar/bridge 一致）。"""

    action: str = Field(..., description="approve | reject | edit | resume")
    edited_arguments: Optional[Dict[str, Any]] = Field(
        None, description="action=edit 时的编辑后参数（与原参数合并）"
    )
    reason: str = Field("", description="裁决备注（落闸门 note）")
    by: str = Field("ops", description="裁决人标识")


@router.get("/hitl/approvals")
async def list_hitl_approvals() -> Dict[str, Any]:
    """HITL 待审面 + 观测面（A4：`bridge.decide` 此前零生产调用方 ⇒ 挂起不可解）。

    惰性 import：测试可 reset 全局桥单例（与 /ops/events 同模式）。
    """
    from agentix.policies.hitl.hitl_bridge import get_hitl_bridge

    bridge = get_hitl_bridge()
    return {"pending": bridge.pending(), "stats": bridge.stats()}


@router.post("/hitl/approvals/{sidecar_request_id}")
async def decide_hitl_approval(
    sidecar_request_id: str, request: ApprovalDecisionRequest
) -> Dict[str, Any]:
    """四裁决落地 → 回写闸门（approve/edit/resume 放行，reject fail-closed）。

    未知 sidecar id ⇒ 404；EDIT 缺参数等裁决错误 ⇒ 400（闸门不落判定）。
    """
    from agentix.policies.hitl.sidecar import ReviewDecisionAction
    from agentix.policies.hitl.hitl_bridge import get_hitl_bridge

    try:
        action = ReviewDecisionAction(request.action)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid action: {request.action}. "
                "Must be one of: approve, reject, edit, resume"
            ),
        )
    bridge = get_hitl_bridge()
    decision = await bridge.decide(
        sidecar_request_id,
        action,
        edited_arguments=request.edited_arguments,
        reason=request.reason,
        by=request.by,
    )
    if decision.not_found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown sidecar request: {sidecar_request_id}",
        )
    if decision.error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=decision.error
        )
    return {
        "gate_request_id": decision.gate_request_id,
        "sidecar_request_id": decision.sidecar_request_id,
        "action": decision.action.value,
        "result_action": decision.result_action.value,
        "approved": decision.approved,
        "merged_arguments": decision.merged_arguments,
        "reason": decision.reason,
        "gate_recovered": decision.gate_recovered,
    }


# ── F2a·M-P2-4 画像作用域（授权边界 + 一键清除全链路）─────────────────────


class ProfileScopeRequest(BaseModel):
    """画像跨会话沿用授权写入体。"""

    user_id: str = Field("default_user", description="目标用户")
    grant: bool = Field(True, description="true=授权跨会话沿用；false=撤销")


class ClearUserMemoryRequest(BaseModel):
    """一键清除画像链请求体。"""

    user_id: str = Field("default_user", description="要清除的用户")


@router.get("/memory/profile-scope")
async def get_profile_scope(
    user_id: str = Query("default_user", description="目标用户"),
) -> Dict[str, Any]:
    """M-P2-4 授权边界读取面：flag 态 + 跨会话沿用授权态（read-only）。"""
    from agentix.application.services.long_term_memory import get_long_term_memory
    from agentix.config.settings import settings

    ltm = get_long_term_memory()
    consent = await ltm.profile_scope_consent(user_id)
    return {
        "user_id": user_id,
        "profile_scope_enabled": bool(settings.MEMORY_PROFILE_SCOPE_ENABLED),
        "cross_session_consent": consent,
    }


@router.post("/memory/profile-scope")
async def set_profile_scope(request: ProfileScopeRequest) -> Dict[str, Any]:
    """M-P2-4 授权写入/撤销面（状态变化由 LTM 侧审计留痕）。"""
    from agentix.application.services.long_term_memory import get_long_term_memory

    ltm = get_long_term_memory()
    await ltm.set_profile_scope_consent(request.user_id, granted=request.grant)
    return {
        "user_id": request.user_id,
        "cross_session_consent": request.grant,
    }


@router.post("/memory/clear-user")
async def clear_user_memory_chain(request: ClearUserMemoryRequest) -> Dict[str, Any]:
    """M-P2-4 一键清除：画像 + 语义卡 + 摘要全链路（分段尝试后汇总）。

    任一段失败 ⇒ 汇总 ``errors`` 显式 500（删除面不静默吞错——清除结果
    必须如实报告，部分成功可见）。
    """
    from agentix.application.services.long_term_memory import get_long_term_memory

    ltm = get_long_term_memory()
    report = await ltm.clear_user_profile_chain(request.user_id)
    if report.get("errors"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "message": "clear_user_profile_chain partial failure",
                "report": report,
            },
        )
    return report
