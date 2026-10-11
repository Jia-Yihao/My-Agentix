"""HTTP 中间件（P0-4 可观测性接线）。

TraceIdMiddleware：为每个请求分配/透传 trace_id，贯穿 HTTP → Agent → LLM。
- 客户端带 X-Trace-Id 请求头则沿用（跨服务串联），否则生成 uuid4
- 响应头回写 X-Trace-Id，日志与指标通过 trace_id_var 读取同一值

P2-2：``trace_id_var`` 的真实定义已下沉到 ``agentix.domain.tracing``
（application 层也要读取它，放在这里会导致 application → interfaces 反向
依赖）。本模块 **re-export** 同一对象，旧导入路径
``from agentix.interfaces.http.middleware import trace_id_var`` 保持可用且
与新路径是**同一个 ContextVar**（identity 相同）。
"""

import uuid

from starlette.middleware.base import BaseHTTPMiddleware

from agentix.domain.tracing import get_trace_id, trace_id_var

__all__ = ["TraceIdMiddleware", "trace_id_var", "get_trace_id"]


class TraceIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        tid = request.headers.get("X-Trace-Id") or uuid.uuid4().hex
        trace_id_var.set(tid)
        # R4.6 融合缺口修复：R1 迁入的 retrieval 路由统一以
        # `request.state.request_id` 构造响应（原 Tracerag 后端有独立
        # RequestID 中间件，融合后未带上）→ 此前所有 /api/v1/retrieval/**
        # 请求均 500。trace_id 与 request_id 本就是同一链路 ID，复用同一值。
        request.state.request_id = tid
        response = await call_next(request)
        response.headers["X-Trace-Id"] = tid
        return response
