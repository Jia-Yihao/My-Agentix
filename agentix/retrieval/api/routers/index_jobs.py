"""
索引任务路由
"""
from fastapi import APIRouter, BackgroundTasks, Request, Depends
from sqlmodel import Session

from agentix.retrieval.api.authz import require_document_readable, require_index_job_readable
from agentix.retrieval.api.deps import DependsSession, get_index_service, get_current_user
from agentix.retrieval.api.schemas.index_jobs import IndexJobTimingConfigData
from agentix.retrieval.domain.models.user import User
from agentix.retrieval.api.response import success, get_status_code, ErrorCode
from agentix.retrieval.infra.repos.index_repo import (
    STALE_QUEUED_SECONDS,
    STALE_RUNNING_SECONDS,
)
from agentix.retrieval.settings import settings
from agentix.retrieval.usecases.index_service import IndexService
from agentix.retrieval.errors import AppError

router = APIRouter(dependencies=[Depends(get_current_user)])

# 「停留过久可重试」提示线：僵尸判定线 + 缓冲（与前端旧默认 35min 同口径），避免
# 提示刚出现用户就去点重试、而后端还没把任务判为僵尸（会吃 400）。
STALL_HINT_BUFFER_SECONDS = 300


@router.get("/index-jobs/timing-config")
def api_get_index_job_timing_config(request: Request):
    """索引任务时序配置（生效值）：前端据此计算「任务停滞」提示，避免写死阈值后与后端漂移。"""
    return success(
        data=IndexJobTimingConfigData(
            job_timeout_seconds=settings.INDEX_JOB_TIMEOUT_SECONDS,
            stale_queued_seconds=STALE_QUEUED_SECONDS,
            stale_running_seconds=STALE_RUNNING_SECONDS,
            stall_hint_seconds=STALE_RUNNING_SECONDS + STALL_HINT_BUFFER_SECONDS,
        ),
        request_id=request.state.request_id,
    )


@router.post("/index-jobs/{job_id}/retry")
def api_retry_index_job(
    request: Request,
    job_id: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    session: Session = DependsSession,
    svc: IndexService = Depends(get_index_service),
):
    require_index_job_readable(session, job_id, current_user.id)
    try:
        data = svc.retry_job(job_id=job_id, background_tasks=background_tasks)
    except ValueError as exc:
        raise AppError(
            error_code=ErrorCode.VALIDATION_ERROR,
            message=str(exc),
            status_code=get_status_code(ErrorCode.VALIDATION_ERROR),
        ) from exc
    if not data:
        raise AppError(
            error_code=ErrorCode.JOB_NOT_FOUND,
            message="index job not found",
            status_code=get_status_code(ErrorCode.JOB_NOT_FOUND),
            details={"job_id": job_id},
        )
    return success(data=data, request_id=request.state.request_id)


@router.get("/index-jobs/{job_id}")
def api_get_index_job(
    request: Request,
    job_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = DependsSession,
    svc: IndexService = Depends(get_index_service),
):
    require_index_job_readable(session, job_id, current_user.id)

    data = svc.get_job(job_id=job_id)
    if not data:
        raise AppError(
            error_code=ErrorCode.JOB_NOT_FOUND,
            message="index job not found",
            status_code=get_status_code(ErrorCode.JOB_NOT_FOUND),
            details={"job_id": job_id},
        )
    return success(data=data, request_id=request.state.request_id)


@router.get("/index-jobs/documents/{document_id}/index-job")
def api_get_document_index_job(
    request: Request,
    document_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = DependsSession,
    svc: IndexService = Depends(get_index_service),
):
    require_document_readable(session, document_id, current_user.id)

    data = svc.get_document_latest_job(document_id=document_id)
    if not data:
        raise AppError(
            error_code=ErrorCode.JOB_NOT_FOUND,
            message="index job not found",
            status_code=get_status_code(ErrorCode.JOB_NOT_FOUND),
            details={"document_id": document_id},
        )
    return success(data=data, request_id=request.state.request_id)
