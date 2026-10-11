"""任务级 Agent Run API（Control Plane 的 HTTP 化；A1c 上移至接口层）。

把「一次 Agent 运行」从 Chatbot 会话语义升级为一等公民：
``POST /agents/runs`` 派生运行 → ``GET /agents/runs/{run_id}`` 查状态 →
``POST /agents/runs/{run_id}/cancel`` 取消。Chatbot API 之外，
这是 Agent Runtime API 的入口。

A1c（统一执行链）：本模块自 ``agentix/retrieval/api/routers/agent_runs.py``
**上移**至接口层——Run API 是协议转换（HTTP ↔ ControlPlane），按分层
归属 ``interfaces``；路径从 ``/api/v1/retrieval/agents/runs`` 迁为
``/api/v1/agents/runs``（路由路径 ``/agents/runs`` 不变，仅去掉 retrieval
挂载前缀），文档（README / PROJECT_SPEC / failure-recovery runbook）同步。

设计约束（遵守既有纪律）：
- **零 schema 变更**：运行实例只存内存（``ControlPlane`` 有界 FIFO），
  不落库——运行生命周期短，持久化属 v2（对齐 task_queue 的取舍）；
- 执行后端走 :mod:`agentix.application.services.agent_executors` 注册表，
  API 层不关心执行器实现（``probe`` 离线可测 + ``chat`` 真实 Agent 图）；
- 超时语义：``timeout_s`` 由 runner 统一 ``asyncio.wait_for`` 施加，
  超时落 ``FAILED``（error=timeout after Ns），绝不悬挂；
- 取消语义：``ControlPlane.cancel`` 置终态并真正打断底层 Task；
  终态幂等（取消后执行器结果不会覆盖 cancelled）。

鉴权（上移后口径不变：默认拒绝无票请求）：router 级严格 Bearer JWT
（``require_run_bearer``，只验签 + 过期 + type=access，不查库——区别于
retrieval 域 ``get_current_user`` 的 User 行校验；Run API 不消费用户行）。
``AUTH_COOKIE_SESSION_FALLBACK`` 的会话 Cookie 回退**不适用**于本路由
（接口层不依赖 persistence；需要 Cookie 的调用方改带 Bearer 头）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agentix.application.services.agent_executors import (
    executor_names,
    get_executor,
)
from agentix.runtime.control_plane import (
    RunState,
    get_control_plane,
)
from agentix.application.services.token_budget import TokenBudget
from agentix.security.jwt import safe_decode_access_token

logger = logging.getLogger("agentix.interfaces.runs")

#: 默认单次运行超时（秒）——必须有限，防止后台任务悬挂
DEFAULT_TIMEOUT_S = 120.0
#: timeout_s 上限（防呆）
MAX_TIMEOUT_S = 3600.0


def require_run_bearer(request: Request) -> None:
    """严格 Bearer JWT 校验（上移前 retrieval ``get_current_user`` 的默认口径）。

    无 token / token 无效或过期 ⇒ 401。不查库（Run API 不消费用户行，
    v1 轻量域纪律）；``V1_REQUIRE_JWT`` 不 gate 本路由——上移前它就是
    默认拒绝，上移后不得出现「默认开→默认关」的鉴权倒退。
    """
    auth = request.headers.get("Authorization", "")
    token = ""
    parts = auth.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        token = parts[1].strip()
    if not token or safe_decode_access_token(token) is None:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


router = APIRouter(dependencies=[Depends(require_run_bearer)])


class AgentRunCreate(BaseModel):
    """``POST /agents/runs`` 请求体。"""

    agent: str = Field(..., min_length=1, description="执行器名（见 executor_names()）")
    task: str = Field(..., min_length=1, description="任务描述/输入")
    options: Dict[str, Any] = Field(default_factory=dict, description="透传给执行器的选项")
    timeout_s: float = Field(default=DEFAULT_TIMEOUT_S, gt=0, description="单次运行超时（秒）")
    max_steps: int = Field(default=0, ge=0, description="步数预算（0=不限额）")


def _rid(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _success(request: Request, data: Any) -> Dict[str, Any]:
    """成功响应（与上移前 retrieval ``success()`` 信封逐字段一致）。"""
    return {"request_id": _rid(request), "data": data}


def _error(
    request: Request,
    *,
    status_code: int,
    error_code: str,
    message: str,
    user_hint: str = "",
) -> JSONResponse:
    """错误响应（顶层 ``error_code``，与上移前 AppError 信封一致）。"""
    body: Dict[str, Any] = {
        "request_id": _rid(request),
        "error_code": error_code,
        "message": message,
    }
    if user_hint:
        body["user_hint"] = user_hint
    return JSONResponse(status_code=status_code, content=body)


async def _execute_run(
    run_id: str,
    agent_name: str,
    task: str,
    options: Dict[str, Any],
    timeout_s: float,
) -> None:
    """后台执行协程：running → 终态。终态迁移幂等由 ControlPlane 保证。"""
    plane = get_control_plane()
    plane.mark_running(run_id)
    try:
        executor = get_executor(agent_name)
        coro = executor(task, dict(options))
        if timeout_s and timeout_s > 0:
            result = await asyncio.wait_for(coro, timeout=timeout_s)
        else:
            result = await coro
        plane.finish(run_id, state=RunState.SUCCEEDED)
        run = plane.status(run_id)
        if run is not None:
            run.meta["result"] = result
    except asyncio.TimeoutError:
        plane.finish(run_id, state=RunState.FAILED, error=f"timeout after {timeout_s}s")
    except asyncio.CancelledError:
        # cancel() 已置 CANCELLED 终态；finish 幂等也不会覆盖。
        # 这里**重新抛出**以正确结束被取消的任务（asyncio 契约）。
        raise
    except Exception as exc:  # noqa: BLE001 - 执行器异常统一落 FAILED
        plane.finish(run_id, state=RunState.FAILED, error=str(exc))
        logger.warning("agent run failed: run=%s agent=%s error=%s", run_id, agent_name, exc)


@router.post("/agents/runs")
async def api_create_agent_run(request: Request, body: AgentRunCreate):
    """派生一次 Agent 运行（任务级入口，Chatbot 会话语义之外）。"""
    timeout_s = min(float(body.timeout_s), MAX_TIMEOUT_S)
    try:
        get_executor(body.agent)
    except KeyError:
        return _error(
            request,
            status_code=400,
            error_code="UNKNOWN_AGENT",
            message=f"unknown agent executor: {body.agent}",
            user_hint=f"可用执行器：{', '.join(executor_names())}",
        )

    # Phase H · W3：准入三态（flag 关直通）；拒绝抛 AdmissionDenied →
    # 全局异常处理器映射 429（fail-closed，不登记不执行）。
    from agentix.performance.admission import admission_wait

    await admission_wait(kind="agent_run")

    plane = get_control_plane()
    budget = (
        TokenBudget(limit=int(body.max_steps), name="agent_run_steps")
        if body.max_steps > 0
        else None
    )
    run = plane.spawn(
        body.agent,
        budget=budget,
        trace_id=getattr(request.state, "request_id", "-"),
        meta={"task_preview": body.task[:200], "timeout_s": timeout_s},
    )
    worker = asyncio.create_task(
        _execute_run(run.run_id, body.agent, body.task, body.options, timeout_s)
    )
    plane.bind(run.run_id, worker)
    return _success(
        request,
        {"run_id": run.run_id, "agent": body.agent, "status": run.state.value},
    )


@router.get("/agents/runs")
async def api_list_agent_runs(request: Request, state: Optional[str] = None):
    """列出运行实例（可按状态过滤）+ 控制面统计。"""
    plane = get_control_plane()
    state_filter: Optional[RunState] = None
    if state:
        try:
            state_filter = RunState(state)
        except ValueError:
            return _error(
                request,
                status_code=400,
                error_code="VALIDATION_ERROR",
                message=f"invalid state filter: {state}",
                user_hint="state 取值：pending/running/succeeded/failed/cancelled",
            )
    runs = plane.list_runs(state=state_filter)
    return _success(
        request,
        {
            "runs": [r.to_dict() for r in runs],
            "stats": plane.stats(),
        },
    )


@router.get("/agents/runs/{run_id}")
async def api_get_agent_run(request: Request, run_id: str):
    """查询运行状态（含预算归属与结果 meta）。"""
    run = get_control_plane().status(run_id)
    if run is None:
        return _error(
            request,
            status_code=404,
            error_code="AGENT_RUN_NOT_FOUND",
            message=f"agent run {run_id} not found",
            user_hint="运行不存在或已被淘汰（终态记录按 FIFO 有界保留）",
        )
    return _success(request, run.to_dict())


@router.post("/agents/runs/{run_id}/cancel")
async def api_cancel_agent_run(request: Request, run_id: str):
    """取消运行：置终态并真正打断底层任务（幂等）。"""
    plane = get_control_plane()
    run = plane.status(run_id)
    if run is None:
        return _error(
            request,
            status_code=404,
            error_code="AGENT_RUN_NOT_FOUND",
            message=f"agent run {run_id} not found",
            user_hint="运行不存在或已被淘汰（终态记录按 FIFO 有界保留）",
        )
    await plane.cancel(run_id, reason="user_cancelled")
    refreshed = plane.status(run_id)
    return _success(request, (refreshed or run).to_dict())
