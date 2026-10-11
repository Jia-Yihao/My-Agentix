"""
审批路由：待审批清单 / 详情 / 通过 / 驳回

权限：
- 审批人（settings.DELETE_APPROVERS 含其 user_id 或 email）：可查全部待审批清单、执行审批；
- 普通用户：仅可查自己发起的审批状态（按 requested_by 过滤）。
"""
from fastapi import APIRouter, Request, Depends, BackgroundTasks

from sqlmodel import Session

from agentix.retrieval.api.deps import get_current_user, DependsSession, get_index_service
from agentix.retrieval.api.response import success, ErrorCode
from agentix.retrieval.domain.models.user import User
from agentix.retrieval.errors import AppError
from agentix.retrieval.settings import settings
from agentix.retrieval.usecases import approval_service
from agentix.retrieval.usecases.index_service import IndexService

router = APIRouter(dependencies=[Depends(get_current_user)])


def _require_approver(current_user: User) -> None:
    approvers = set(settings.DELETE_APPROVERS)
    if current_user.id in approvers or current_user.email in approvers:
        return
    raise AppError(
        error_code=ErrorCode.FORBIDDEN,
        message="无审批权限",
        status_code=403,
    )


@router.get("/approvals")
def api_list_approvals(
    request: Request,
    scope: str = "mine",
    limit: int = 100,
    current_user: User = Depends(get_current_user),
    session: Session = DependsSession,
):
    """列出审批记录。

    - scope=mine（默认）：当前用户发起的；
    - scope=pending：审批人视角D待审批清单（需审批权限）。
    """
    if scope == "pending":
        _require_approver(current_user)
        data = approval_service.list_pending_approvals(session, limit=limit)
    else:
        data = approval_service.list_pending_approvals(
            session, requested_by=current_user.id, limit=limit
        )
    return success(data=data, request_id=request.state.request_id)


@router.get("/approvals/{approval_id}")
def api_get_approval(
    request: Request,
    approval_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = DependsSession,
):
    data = approval_service.get_approval_detail(session, approval_id)
    # 普通用户仅可查自己发起的；审批人可查任意
    if data.get("requested_by") != current_user.id:
        _require_approver(current_user)
    return success(data=data, request_id=request.state.request_id)


@router.post("/approvals/{approval_id}/approve")
def api_approve(
    request: Request,
    approval_id: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    session: Session = DependsSession,
    index_svc: IndexService = Depends(get_index_service),
):
    _require_approver(current_user)
    data = approval_service.approve_approval(
        session, approval_id, approved_by=current_user.id
    )
    if data.get("should_enqueue") and data.get("job_id"):
        index_svc.enqueue_index_job(
            job_id=data["job_id"], background_tasks=background_tasks
        )
    return success(data=data, request_id=request.state.request_id)


@router.post("/approvals/{approval_id}/reject")
def api_reject(
    request: Request,
    approval_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = DependsSession,
):
    _require_approver(current_user)
    data = approval_service.reject_approval(
        session, approval_id, approved_by=current_user.id
    )
    return success(data=data, request_id=request.state.request_id)
