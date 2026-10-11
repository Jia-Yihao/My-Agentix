"""
API v1 router aggregation.

逐端点鉴权（2026-09-27 审查批次；审计批次 A2 修订）：
- 用户面 chat / sessions / upload（**含读**）→ ``Depends(require_v1_user)``
  （``V1_REQUIRE_JWT`` 开启时要求 Bearer JWT，默认关保持轻量匿名域）；
- 机器面 events → ``Depends(verify_api_key)``（``REQUIRE_API_KEY`` 管辖）；
- Run API runs（A1c 上移）→ router 级严格 Bearer JWT
  （``runs.require_run_bearer``，默认拒绝，不查库）。
归属校验（审计批次 A1/A2）：``resolve_v1_caller`` + ``session_visible_to``
（见 `v1/deps.py`），跨用户 404。
"""
from fastapi import APIRouter

from agentix.config import settings
from agentix.interfaces.http.v1 import runs, sessions, chat, upload, events

api_router = APIRouter()

# Include routers（sessions/chat/upload/events 免前置鉴权，逐端点 deps；
# runs 携带 router 级严格 Bearer JWT——见 runs.require_run_bearer）
api_router.include_router(sessions.router, prefix="/sessions", tags=["Chat Sessions"])
api_router.include_router(chat.router, prefix="/chat", tags=["Unified Chat"])
api_router.include_router(upload.router, prefix="/upload", tags=["File Upload"])
api_router.include_router(events.router, prefix="/events", tags=["External Events"])
# A1c：Run API 上移（原 /api/v1/retrieval/agents/runs → /api/v1/agents/runs）
api_router.include_router(runs.router, tags=["Agent Runs"])

# P2-1 满意度反馈（flag-off 路由不注册 = 与基线逐位等价，404 语义）
if settings.FEEDBACK_ENABLED:
    from agentix.interfaces.http.v1 import feedback

    api_router.include_router(feedback.router, prefix="/feedback", tags=["Feedback"])
