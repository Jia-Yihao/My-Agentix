"""MCP stdio 客户端 E2E：spawn 真实 server 子进程全链路。

覆盖：握手 → tools/list → tools/call → ToolExecutor 适配注册 →
经统一执行器调用远端工具；超时保护。
"""

from __future__ import annotations

import sys

import pytest

from agentix.tools.definitions import registry as tool_registry
from agentix.tools.gateway.executor import ToolExecutor
from agentix.capabilities.mcp.client import MCPStdioClient, MCPTimeoutError

pytestmark = pytest.mark.asyncio

SERVER_CMD = [sys.executable, "-m", "agentix.capabilities.mcp.server"]


async def test_end_to_end_handshake_list_call():
    async with MCPStdioClient(SERVER_CMD) as client:
        assert client.server_info["serverInfo"]["name"] == "agentix-mcp-server"

        tools = await client.list_tools()
        names = {t["name"] for t in tools}
        assert {"calculator", "text_stats"} <= names

        payload = await client.call_tool("calculator", {"expression": "6*7"})
        assert payload.get("isError") is not True
        assert payload["content"][0]["type"] == "text"
        assert '"value": 42' in payload["content"][0]["text"]


async def test_register_into_executor_and_execute_via_unified_governance():
    async with MCPStdioClient(SERVER_CMD) as client:
        executor = ToolExecutor()
        registered = await client.register_into(executor)

        assert "mcp_calculator" in registered
        assert tool_registry.has_tool("mcp_calculator")
        spec = tool_registry.get_spec("mcp_calculator")
        # 默认**保守建模**：远端工具的能力在协议层不可知，因此默认按 L2
        # （需人工确认）+ side_effect=True 建模，而不是"声明成 L0 只读"。
        # 想让它变成只读，必须由调用方显式声明（见下一个测试）。
        assert spec.risk_level == "L2", "远端工具不得被默认为只读"
        assert spec.side_effect is True
        assert spec.control.strip(), "L2 必须带 control 控制措施说明"

        result = await executor.execute("mcp_calculator", {"expression": "1+2"})
        assert result.success is True
        content = result.data
        assert content[0]["type"] == "text"

        # 工具级失败 → 统一落 ToolResult(success=False)，不抛异常
        bad = await executor.execute("mcp_calculator", {"expression": "import os"})
        assert bad.success is False
        assert bad.retryable is False

    # 清理全局注册表（防测试间污染）
    for name in list(registered):
        tool_registry.delete_tool(name)


async def test_register_into_explicit_l0_optin_is_the_only_way_to_readonly():
    """显式声明 L0 是**唯一**能把远端工具变成只读的路径（默认绝不放行）。"""
    async with MCPStdioClient(SERVER_CMD) as client:
        executor = ToolExecutor()
        registered = await client.register_into(
            executor, risk_level_by_tool={"calculator": "L0"}
        )

        spec = tool_registry.get_spec("mcp_calculator")
        assert spec.risk_level == "L0" and spec.side_effect is False
        assert spec.control == "", "L0 不需要控制措施说明"

        # 未在映射里声明的另一个工具仍按默认 L2 保守建模
        other = tool_registry.get_spec("mcp_text_stats")
        assert other.risk_level == "L2" and other.side_effect is True

    for name in list(registered):
        tool_registry.delete_tool(name)


async def test_request_timeout_raises():
    """对不回话的子进程发起握手 → MCPTimeoutError（不悬挂）。"""
    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
    client = MCPStdioClient(sleeper, request_timeout_s=0.5)
    with pytest.raises(MCPTimeoutError):
        await client.start()
    await client.aclose()
