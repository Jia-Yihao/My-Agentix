"""
当前用户
"""
from fastapi import APIRouter, Request, Depends

from agentix.retrieval.api.deps import get_current_user
from agentix.retrieval.api.response import success
from agentix.retrieval.api.schemas.auth import UserPublic
from agentix.retrieval.domain.models.user import User

router = APIRouter()


@router.get("/users/me")
def api_me(request: Request, current_user: User = Depends(get_current_user)):
    data = UserPublic(
        id=current_user.id,
        email=current_user.email,
        username=current_user.username,
        tenant_id=current_user.tenant_id,
        role=current_user.role,
        status=current_user.status,
    ).model_dump()
    return success(data=data, request_id=request.state.request_id)
