"""MCP Server——stdio JSON-RPC 2.0 服务端（MCP 2024-11-05 协议子集）。

零依赖实现（不引入 mcp SDK）：逐行读 stdin、逐行写 stdout，
消息格式为单行 JSON（``\\n`` 分帧）。支持方法：

- ``initialize``        → 协议版本 + 服务端能力 + serverInfo
- ``notifications/initialized``（通知，不响应）
- ``ping``              → {}
- ``tools/list``        → {tools: [{name, description, inputSchema}]}
- ``tools/call``        → {content: [{type: text, text}], isError}

运行命令（Claude Desktop / 任何 MCP 宿主均可直接挂载）::

    python -m agentix.capabilities.mcp.server                 # 默认 OfflineToolSource
    python -m agentix.capabilities.mcp.server --help          # 见 parse_args
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any, Dict, Optional

from agentix.capabilities.mcp.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    MCPError,
    PROTOCOL_VERSION,
    error_response,
    parse_message,
    result_response,
)
from agentix.capabilities.mcp.tool_source import OfflineToolSource, StaticToolSource

logger = logging.getLogger("agentix.capabilities.mcp.server")

SERVER_INFO = {"name": "agentix-mcp-server", "version": "0.1.0"}


class MCPServer:
    """协议分发核心（与传输解耦：handle_request 可单测，serve 管stdio）。"""

    def __init__(self, tool_source: Optional[StaticToolSource] = None) -> None:
        self._source = tool_source or OfflineToolSource()

    @property
    def tool_source(self) -> StaticToolSource:
        return self._source

    async def handle_request(self, raw: str) -> Optional[Dict[str, Any]]:
        """处理一行请求；通知返回 ``None``，其余返回响应 dict。"""
        try:
            msg = parse_message(raw)
        except MCPError as exc:
            return error_response(None, exc.code, exc.message)

        if msg is None:  # 通知：无需响应
            return None

        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}
        try:
            result = await self._dispatch(method, params)
        except MCPError as exc:
            return error_response(req_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # noqa: BLE001 - 工具/内部异常统一 -32603
            logger.warning("mcp internal error on {}: {}", method, exc)
            return error_response(req_id, INTERNAL_ERROR, f"internal error: {exc}")
        return result_response(req_id, result)

    async def _dispatch(self, method: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        if method == "initialize":
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self._source.list_tools()}
        if method == "tools/call":
            return await self._call_tool(params)
        raise MCPError(METHOD_NOT_FOUND, f"method not found: {method}")

    async def _call_tool(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise MCPError(INVALID_PARAMS, "params.name (string) is required")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise MCPError(INVALID_PARAMS, "params.arguments must be an object")
        try:
            value = await self._source.call_tool(name, arguments)
        except ValueError as exc:  # 参数/表达式错误 → 工具级失败（isError，非协议错误）
            return _tool_text_response(str(exc), is_error=True)
        return _tool_text_response(value)

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """asyncio 流循环（serve_stdio 用；测试可直接喂内存流）。"""
        while True:
            line = await reader.readline()
            if not line:
                break
            response = await self.handle_request(line.decode("utf-8"))
            if response is not None:
                writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
                await writer.drain()

    def serve_sync(self, stdin: Any = None, stdout: Any = None) -> None:
        """同步 stdio 循环（MCP 宿主入口）。

        不用 asyncio 连接管道——Windows 上 connect_read_pipe 对 stdio
        支持不稳；逐行 ``asyncio.run`` 处理足够（工具调用均为短任务）。
        """
        stdin = stdin if stdin is not None else sys.stdin
        stdout = stdout if stdout is not None else sys.stdout
        for raw in stdin:
            if not raw.strip():
                continue
            response = asyncio.run(self.handle_request(raw))
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                stdout.flush()


def _tool_text_response(value: Any, *, is_error: bool = False) -> Dict[str, Any]:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    payload: Dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        payload["isError"] = True
    return payload


def main() -> None:  # pragma: no cover - 子进程入口
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    MCPServer().serve_sync()


if __name__ == "__main__":
    main()
