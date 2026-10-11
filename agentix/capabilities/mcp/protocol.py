"""MCP 协议常量与 JSON-RPC 2.0 载荷工具（零依赖实现）。

只实现 My Agentix 需要的 MCP 2024-11-05 子集：initialize / ping /
tools/list / tools/call。协议演进时在此扩展，不动 server/client 逻辑。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

__all__ = [
    "PROTOCOL_VERSION",
    "JSONRPC_VERSION",
    "MCPError",
    "error_response",
    "result_response",
    "parse_message",
]

#: 与官方 SDK 协商一致的协议版本
PROTOCOL_VERSION = "2024-11-05"

JSONRPC_VERSION = "2.0"

# JSON-RPC 2.0 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class MCPError(Exception):
    """携带 JSON-RPC 错误码与 message 的协议错误。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def parse_message(raw: str) -> Optional[Dict[str, Any]]:
    """解析一行 JSON-RPC 消息；非法 JSON 抛 PARSE_ERROR。

    返回 ``None`` 表示通知（无 id，不需要响应）。
    """
    import json

    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise MCPError(PARSE_ERROR, f"parse error: {exc}") from exc
    if not isinstance(msg, dict) or msg.get("jsonrpc") != JSONRPC_VERSION:
        raise MCPError(INVALID_REQUEST, "invalid request: jsonrpc must be '2.0'")
    if "id" not in msg:
        return None  # 通知：initialized 等
    return msg


def result_response(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "result": result}


def error_response(req_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "error": err}
