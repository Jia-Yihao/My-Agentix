"""
租户路由——管理员 CRUD 租户（spec §5.5.1 / §6.1）。

写操作（创建/更新/删除）要求 admin 角色；读操作（列表/详情）要求登录。
"""
from fastapi import APIRouter, Request, Depends

from agentix.retrieval.api.deps import get_tenant_service, get_current_user, get_current_admin
from agentix.retrieval.api.response import success, get_status_code, ErrorCode
from agentix.retrieval.api.schemas.tenant import TenantCreateRequest, TenantUpdateRequest
from agentix.retrieval.usecases.tenant_service import TenantService
from agentix.retrieval.errors import AppError

router = APIRouter(dependencies=[Depends(get_current_user)])


@router.post("/tenants")
def api_create_tenant(
    request: Request,
    req: TenantCreateRequest,
    current_admin=Depends(get_current_admin),
    svc: TenantService = Depends(get_tenant_service),
):
    data = svc.create_tenant(
        name=req.name,
        actor_user_id=current_admin.id,
        actor_tenant_id=current_admin.tenant_id,
    )
    return success(data=data, request_id=request.state.request_id)


@router.get("/tenants")
def api_list_tenants(
    request: Request,
    page: int = 1,
    page_size: int = 20,
    svc: TenantService = Depends(get_tenant_service),
):
    data = svc.list_tenants(page=page, page_size=page_size)
    return success(data=data, request_id=request.state.request_id)


@router.get("/tenants/{tenant_id}")
def api_get_tenant(
    request: Request,
    tenant_id: str,
    svc: TenantService = Depends(get_tenant_service),
):
    data = svc.get_tenant(tenant_id=tenant_id)
    if not data:
        raise AppError(
            error_code=ErrorCode.TENANT_NOT_FOUND,
            message="租户不存在",
            status_code=get_status_code(ErrorCode.TENANT_NOT_FOUND),
        )
    return success(data=data, request_id=request.state.request_id)


@router.patch("/tenants/{tenant_id}")
def api_update_tenant(
    request: Request,
    tenant_id: str,
    req: TenantUpdateRequest,
    current_admin=Depends(get_current_admin),
    svc: TenantService = Depends(get_tenant_service),
):
    data = svc.update_tenant(
        tenant_id=tenant_id,
        name=req.name,
        status=req.status,
        actor_user_id=current_admin.id,
        actor_tenant_id=current_admin.tenant_id,
    )
    if not data:
        raise AppError(
            error_code=ErrorCode.TENANT_NOT_FOUND,
            message="租户不存在",
            status_code=get_status_code(ErrorCode.TENANT_NOT_FOUND),
        )
    return success(data=data, request_id=request.state.request_id)


@router.delete("/tenants/{tenant_id}")
def api_delete_tenant(
    request: Request,
    tenant_id: str,
    current_admin=Depends(get_current_admin),
    svc: TenantService = Depends(get_tenant_service),
):
    data = svc.delete_tenant(
        tenant_id=tenant_id,
        actor_user_id=current_admin.id,
        actor_tenant_id=current_admin.tenant_id,
    )
    if not data:
        raise AppError(
            error_code=ErrorCode.TENANT_NOT_FOUND,
            message="租户不存在",
            status_code=get_status_code(ErrorCode.TENANT_NOT_FOUND),
        )
    return success(data=data, request_id=request.state.request_id)
