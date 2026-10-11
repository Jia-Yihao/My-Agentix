"""Agent 执行器注册表——任务级 Agent Run API 的执行后端。

职责边界（收口而非重构）：
- ``control_plane`` 管运行**登记 + 取消 + 预算归属**（不做调度）；
- 本模块管「拿到一个任务后**怎么跑完**」——每个注册名对应一个
  ``async executor(task: str, options: dict) -> dict``。

执行器契约::

    async def executor(task: str, options: dict) -> dict:
        return {"output": <最终结果>, "meta": {...}}   # 正常完成
    # 超时由 runner 统一用 asyncio.wait_for 施加；执行器内部异常
    # 由 runner 捕获并落 ControlPlane 终态（FAILED + error），不外抛。

内置执行器：
- ``probe``：回显任务 + 可选延迟（``options.delay_s``）。定位是
  **控制面契约验证**——spawn/状态迁移/超时/取消语义的端到端演示，
  离线可测，不依赖任何外部服务。
- ``chat``（A1c 真实 Agent 接线）：把 ``(task, options)`` 映射为
  ``ChatService.ask`` 主链路（真实 LangGraph 图）。``options.session_id``
  可指定会话语义；缺省自生成一次性 thread id。API 层契约不变。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Callable, Dict, List

__all__ = [
    "Executor",
    "register_executor",
    "get_executor",
    "executor_names",
    "probe_executor",
    "chat_executor",
]

#: 执行器签名：``async (task, options) -> {"output": ..., "meta": {...}}``
Executor = Callable[[str, Dict[str, Any]], Any]

_EXECUTORS: Dict[str, Executor] = {}


def register_executor(name: str, fn: Executor, *, overwrite: bool = False) -> None:
    """注册一个执行器。默认禁止覆盖（防测试间互相污染 / 意外替换）。"""
    key = str(name)
    if not key or key != key.strip():
        raise ValueError(f"invalid executor name: {name!r}")
    if key in _EXECUTORS and not overwrite:
        raise ValueError(f"executor '{key}' already registered (use overwrite=True to replace)")
    _EXECUTORS[key] = fn


def get_executor(name: str) -> Executor:
    """按名取执行器；未注册抛 ``KeyError``（路由层转 UNKNOWN_AGENT 400）。"""
    return _EXECUTORS[str(name)]


def executor_names() -> List[str]:
    return list(_EXECUTORS.keys())


# ---------------------------------------------------------------------------
# 内置执行器
# ---------------------------------------------------------------------------

async def probe_executor(task: str, options: Dict[str, Any]) -> Dict[str, Any]:
    """回显执行器：``options.delay_s`` 秒后返回任务原文。

    延迟期可被 cancel 真正打断（asyncio.sleep 对取消敏感），
    用于验证 spawn → running → cancel / timeout 全链路。
    """
    delay = float(options.get("delay_s", 0.0) or 0.0)
    if delay > 0:
        await asyncio.sleep(delay)
    return {
        "output": task,
        "meta": {"executor": "probe", "delay_s": delay},
    }


async def chat_executor(task: str, options: Dict[str, Any]) -> Dict[str, Any]:
    """真实 Agent 图执行器（A1c）：``ChatService.ask`` 主链路适配。

    - ``options.session_id``：复用既有会话语义（缺省自生成一次性
      thread id——Run API 不经端点层，不落会话消息，只跑图）；
    - ``options.image_path`` / ``options.file_path`` /
      ``options.ingest_incremental``：透传给 ``ask``；
    - 返回契约不变：``{"output": 回答正文, "meta": {...}}``。
      ``ask`` 的可降级错误（200 + status/error_code）不抛异常——
      正文与 ``error_code`` 一并落 meta，由调用方呈现。
    """
    from agentix.application.services.chat_service import get_chat_service

    session_id = str(options.get("session_id") or uuid.uuid4().hex)
    result = await get_chat_service().ask(
        task,
        session_id,
        image_path=options.get("image_path"),
        file_path=options.get("file_path"),
        ingest_incremental=options.get("ingest_incremental"),
    )
    return {
        "output": result.get("message") or "",
        "meta": {
            "executor": "chat",
            "session_id": session_id,
            "route": result.get("route"),
            "status": result.get("status"),
            "error_code": result.get("error_code"),
            "sources": result.get("sources") or [],
        },
    }


register_executor("probe", probe_executor)
register_executor("chat", chat_executor)
