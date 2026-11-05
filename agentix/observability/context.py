"""request_id / trace_id 的 ContextVar 存取。

传播规则：
- HTTP 中间件入口 ``bind_request_id``，``finally`` 中 ``reset_request_id``；
- ``asyncio.to_thread`` 自动继承上下文，勿手工传参；
- worker 入口绑定 ``job_<job_id>``。
禁止把 request_id 当参数逐层透传。
"""
from __future__ import annotations

from contextvars import ContextVar, Token

_request_id: ContextVar[str] = ContextVar("retrieval_request_id", default="-")
_trace_id: ContextVar[str] = ContextVar("retrieval_trace_id", default="-")


def bind_request_id(rid: str | None) -> Token:
    """绑定当前请求的 request_id，返回 Token 供 reset。"""
    return _request_id.set(rid or "-")


def current_request_id() -> str:
    return _request_id.get()


def reset_request_id(token: Token) -> None:
    _request_id.reset(token)


def bind_trace_id(tid: str | None) -> Token:
    """绑定当前请求的 trace_id，返回 Token 供 reset。"""
    return _trace_id.set(tid or "-")


def current_trace_id() -> str:
    return _trace_id.get()


def reset_trace_id(token: Token) -> None:
    _trace_id.reset(token)
