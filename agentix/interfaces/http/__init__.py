"""HTTP 接口层路由聚合。

R1.10 后 knowledge_router 已退役，检索路由统一由
agentix.retrieval.api.routers 提供（挂载于 /api/v1/retrieval）。
"""

from . import lightrag_router

__all__ = ["lightrag_router"]
