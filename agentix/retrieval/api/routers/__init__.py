"""Retrieval API 路由聚合——My Agentix 检索能力域的全部 HTTP 路由。

挂载前缀：``/api/v1/retrieval``（避免与 My Agentix 既有 ``/api/v1/chat`` 等冲突）。

A1c（统一执行链）：任务级 Run API（原 ``agent_runs.py``）已上移至
``agentix/interfaces/http/v1/runs.py``，路径迁为 ``/api/v1/agents/runs``——
本聚合不再包含它。
"""

from fastapi import APIRouter

from agentix.retrieval.api.routers.approvals import router as approvals_router
from agentix.retrieval.api.routers.auth import router as auth_router
from agentix.retrieval.api.routers.chat import router as chat_router
from agentix.retrieval.api.routers.conversations import router as conversations_router
from agentix.retrieval.api.routers.documents import router as documents_router
from agentix.retrieval.api.routers.explanations import router as explanations_router
from agentix.retrieval.api.routers.feedback import router as feedback_router
from agentix.retrieval.api.routers.folders import router as folders_router
from agentix.retrieval.api.routers.index_jobs import router as index_jobs_router
from agentix.retrieval.api.routers.kb import router as kb_router
from agentix.retrieval.api.routers.settings import router as settings_router
from agentix.retrieval.api.routers.tenants import router as tenants_router
from agentix.retrieval.api.routers.users import router as users_router
from agentix.retrieval.api.routers.vector_db import router as vector_db_router

router = APIRouter()
router.include_router(auth_router, tags=["auth"])
router.include_router(users_router, tags=["users"])
router.include_router(tenants_router, tags=["tenants"])
router.include_router(kb_router, tags=["knowledge-bases"])
router.include_router(documents_router, tags=["documents"])
router.include_router(folders_router, tags=["folders"])
router.include_router(index_jobs_router, tags=["index-jobs"])
router.include_router(chat_router, tags=["retrieval-chat"])
router.include_router(conversations_router, tags=["conversations"])
router.include_router(feedback_router, tags=["feedback"])
router.include_router(explanations_router, tags=["explanations"])
router.include_router(approvals_router, tags=["approvals"])
router.include_router(settings_router, tags=["settings"])
router.include_router(vector_db_router, tags=["vector-db"])

__all__ = ["router"]
