"""
Observability, breakpoint resume, and HITL approval API endpoints.
"""

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from agentix.application.services.observability import context_report, get_observability
from agentix.application.services.breakpoint_resume import (
    get_breakpoint_manager,
    get_checkpoint_recovery,
    PendingTaskNotFoundError,
    ResumeAction,
    TaskNotRetryableError,
    ThreadInterruptedError,
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
    """List threads with incomplete executions (potential crash victims).

    LG-2-1：线程清单来自 checkpointer 的 alist 枚举，「未完成」由图状态快照
    （next / interrupts）判定——不再读 ``active_threads`` Redis 集合（该集合
    无生产写入点）。
    """
    recovery = get_checkpoint_recovery()
    # 同 /threads/resume：惰性 import 取「当前」图。模块级 import 会在启动期
    # checkpointer 覆盖、图重编译之前固化旧图。
    from agentix.orchestration.langgraph.lg_builder import graph

    return await recovery.get_active_threads(graph)


@router.post("/threads/resume")
async def resume_thread(request: ResumeRequest) -> Dict[str, Any]:
    """Attempt to resume a crashed thread from its last checkpoint.

    状态码口径（LG-2-2）：404 = 没有可续跑 checkpoint；409 = 线程挂着未决中断
    （改走 ``/ops/hitl/in-loop``）；500 = 续跑失败。
    """
    recovery = get_checkpoint_recovery()

    # 说明（非缺陷）：此处为函数体内（惰性）import，保留原样。函数内 `from X import y`
    # 每次执行都会重新读取模块属性，总能取到当前值，语义等价于 `lg_builder.graph`，
    # 因此不受启动期 Redis checkpointer 覆盖的影响。请勿改为模块级 import——
    # 值绑定会在 import 时固化，反而可能拿到启动期覆盖之前的默认图。
    from agentix.orchestration.langgraph.lg_builder import graph

    # LG-2-1：存在性判据从 Redis 元数据换成 checkpoint 快照本身。待恢复线程由
    # checkpointer 派生，不再保证有元数据；用元数据判 404 会把所有线程挡在门外。
    snapshot = await recovery.get_thread_snapshot(request.thread_id, graph)
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No checkpoint found for thread: {request.thread_id}",
        )

    try:
        result = await recovery.resume_thread(request.thread_id, graph, kind="ops")
    except ThreadInterruptedError as exc:
        # LG-2-2：等人工裁决的线程不是崩溃受害者。硬续只会「200 但图又挂回去」，
        # 409（与当前资源状态冲突、重试无意义）是这里唯一正确的状态码。
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "thread_interrupted",
                "message": "线程挂着未决中断，请先经 /ops/hitl/in-loop 裁决后再续跑",
                "thread_id": request.thread_id,
                "pending_interrupts": exc.pending,
                "resume_endpoint": "/ops/hitl/in-loop",
            },
        ) from exc

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


# ── LG-2-4：按 task 的定点查看与重跑 ────────────────────────────────────


class RetryTaskRequest(BaseModel):
    """请求体：要重跑的待执行 task。"""

    task_id: str = Field(..., description="Pending task ID（见 GET /ops/threads/{tid}/tasks）")


@router.get("/threads/{thread_id}/tasks")
async def list_thread_tasks(thread_id: str) -> Dict[str, Any]:
    """列出线程当前待执行的 task（快照 ``tasks``：id / name / error / 中断数）。

    状态码：200 = 线程存在（全部完成时 ``tasks`` 为空表）；404 = 无该线程的
    checkpoint。
    """
    recovery = get_checkpoint_recovery()

    # 同 /threads/resume：函数体内 import 每次重读模块属性，跟随启动期 Redis
    # checkpointer 覆盖（模块级值绑定会固化旧图）。
    from agentix.orchestration.langgraph.lg_builder import graph

    tasks = await recovery.list_pending_tasks(thread_id, graph)
    if tasks is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No checkpoint found for thread: {thread_id}",
        )
    return {"thread_id": thread_id, "tasks": tasks}


@router.post("/threads/{thread_id}/retry-task")
async def retry_thread_task(thread_id: str, request: RetryTaskRequest) -> Dict[str, Any]:
    """重跑线程里失败的某个 task；同节点已完成的兄弟 task 不重放。

    状态码：200 = 已续跑；404 = 线程或 task 不存在；409 = task 已完成（拒绝
    重放）或线程挂着未决中断（改走 ``/ops/hitl/in-loop``）；500 = 续跑时图抛错。
    """
    recovery = get_checkpoint_recovery()

    from agentix.orchestration.langgraph.lg_builder import graph

    try:
        result = await recovery.retry_task(thread_id, request.task_id, graph)
    except PendingTaskNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "pending_task_not_found",
                "message": str(exc),
                "thread_id": thread_id,
                "task_id": request.task_id,
            },
        ) from exc
    except TaskNotRetryableError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "task_not_retryable",
                "message": str(exc),
                "thread_id": thread_id,
                "task_id": request.task_id,
            },
        ) from exc
    except ThreadInterruptedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "thread_interrupted",
                "message": "线程挂着未决中断，请先经 /ops/hitl/in-loop 裁决后再重跑",
                "thread_id": thread_id,
                "pending_interrupts": exc.pending,
                "resume_endpoint": "/ops/hitl/in-loop",
            },
        ) from exc

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retry task",
        )
    return result


# ── LG-2-5：历史回看（只读）──────────────────────────────────────────────


@router.get("/threads/{thread_id}/history")
async def get_thread_history(
    thread_id: str,
    limit: int = Query(50, ge=1, le=500),
) -> Dict[str, Any]:
    """回看线程走过的 checkpoint 步骤序列（新→旧），**只读**。

    数据源是 checkpointer 自己的 ``get_state_history``——步骤序列与图实际走过的
    超级步同源；本端点不写任何状态（调用前后 ``get_state`` 的 checkpoint_id 不变）。

    状态码：200 = 线程存在；404 = 无该线程的 checkpoint；500 = checkpointer
    读取失败（读失败不会被报成「没有历史」）。
    """
    recovery = get_checkpoint_recovery()

    # 同 /threads/resume：函数体内 import 每次重读模块属性，跟随启动期 Redis
    # checkpointer 覆盖（模块级值绑定会固化旧图）。
    from agentix.orchestration.langgraph.lg_builder import graph

    history = await recovery.list_thread_history(thread_id, graph, limit=limit)
    if history is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No checkpoint found for thread: {thread_id}",
        )
    return {"thread_id": thread_id, "count": len(history), "history": history}


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
    # T18 返工：flag-on 时 pending() 会并库读（list_unresolved 是同步 SQLAlchemy
    # 会话），不下放线程会阻塞事件循环——与桥内写/回捞/裁决三路径同口径（纪律：
    # CPU/IO 阻塞调用经 asyncio.to_thread 下放）。stats() 纯内存不动。
    return {
        "pending": await asyncio.to_thread(bridge.pending),
        "stats": bridge.stats(),
    }


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
    body = {
        "gate_request_id": decision.gate_request_id,
        "sidecar_request_id": decision.sidecar_request_id,
        "action": decision.action.value,
        "result_action": decision.result_action.value,
        "approved": decision.approved,
        "merged_arguments": decision.merged_arguments,
        "reason": decision.reason,
        "gate_recovered": decision.gate_recovered,
    }
    # T18 验收⑤：库闭合失败对运维可见（error 日志之外的 API 面标记）。
    # **只在非空时出现该键** ⇒ flag-off / 无故障响应与基线逐键一致（纪律 1）。
    if decision.persist_error:
        body["persist_error"] = decision.persist_error
    return body


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


# ── E2·schema 缓存主动失效（路线图 §2 P0「改表 → 主动清 → 立即可见」）──────


@router.post("/schema-cache/invalidate")
async def invalidate_schema_cache_endpoint(
    database_url: Optional[str] = Query(
        None, description="只清这一个库的 text2sql schema 缓存；省略 = 全清（Neo4j 不受此参数影响）"
    ),
) -> Dict[str, Any]:
    """清掉两套 schema 缓存的旧值，让下一次读取重新去源头取 schema。

    为什么要有这个入口：两套缓存的 TTL 节奏本就不一致（text2sql 默认 0=关、
    Neo4j 默认 300s），靠等 TTL 生效最长要忍一个窗口期。这里把两侧收成一个
    口径，且**不看 TTL 开关**——默认部署（TTL 关）也必须有可用的失效入口，
    否则这个端点只是装饰品。

    与 `/memory/clear-user` 的差别是刻意的：清除用户数据失败要显式 500（结果
    必须如实可信），而清缓存失败**不改变正确性**（缓存迟早会自己过期或重建），
    所以按纪律 2 返回 200 + 可读报告，绝不把辅助设施的抖动打成运维事故。
    """
    from agentix.application.services import schema_cache

    try:
        report = schema_cache.invalidate_schema_caches(database_url)
    except Exception as exc:  # noqa: BLE001 - 兜底：辅助设施绝不打挂运维请求，降级为可读报告
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": not report.get("errors"), **report}
