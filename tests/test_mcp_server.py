"""MCP Server 单元测试：协议分发 + 内置工具 + ToolRegistry 桥接源。

全部进程内（asyncio.run 单发请求），不 spawn 子进程——E2E 见 test_mcp_client.py。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agentix.tools.definitions import ToolSpec
from agentix.tools.gateway.executor import ToolExecutor
from agentix.capabilities.mcp.protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
)
from agentix.capabilities.mcp.server import MCPServer, SERVER_INFO
from agentix.capabilities.mcp.tool_source import RegistryToolSource


def _call(raw: str) -> dict:
    server = MCPServer()
    resp = asyncio.run(server.handle_request(raw))
    assert resp is not None
    return resp


def _call_ok(method: str, params: dict | None = None) -> dict:
    raw = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        ensure_ascii=False,
    )
    resp = _call(raw)
    assert "result" in resp, resp
    return resp["result"]


# -- 协议层 -----------------------------------------------------------------

def test_initialize_returns_protocol_version_and_capabilities():
    result = _call_ok("initialize")
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == SERVER_INFO["name"]
    assert "tools" in result["capabilities"]


def test_ping_returns_empty_result():
    assert _call_ok("ping") == {}


def test_bad_json_returns_parse_error():
    resp = _call("not-json{{")
    assert resp["error"]["code"] == PARSE_ERROR


def test_wrong_jsonrpc_version_rejected():
    resp = _call(json.dumps({"jsonrpc": "1.0", "id": 1, "method": "ping"}))
    assert resp["error"]["code"] == INVALID_REQUEST


def test_unknown_method_returns_method_not_found():
    resp = _call(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "resources/list"}))
    assert resp["error"]["code"] == METHOD_NOT_FOUND
    assert resp["id"] == 7


def test_notification_returns_none():
    raw = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert asyncio.run(MCPServer().handle_request(raw)) is None


# -- 内置工具 ----------------------------------------------------------------

def test_tools_list_contains_builtin_offline_tools():
    result = _call_ok("tools/list")
    names = {t["name"] for t in result["tools"]}
    assert {"calculator", "text_stats"} <= names
    for tool in result["tools"]:
        assert set(tool) == {"name", "description", "inputSchema"}


def test_tools_call_calculator():
    result = _call_ok("tools/call", {"name": "calculator", "arguments": {"expression": "(2+3)*4"}})
    assert result.get("isError") is not True
    payload = json.loads(result["content"][0]["text"])
    assert payload["value"] == 20


def test_tools_call_bad_expression_is_tool_level_error():
    result = _call_ok("tools/call", {"name": "calculator", "arguments": {"expression": "import os"}})
    assert result["isError"] is True
    assert "invalid expression" in result["content"][0]["text"]


def test_tools_call_missing_name_is_protocol_error():
    resp = _call(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {}}))
    assert resp["error"]["code"] == INVALID_PARAMS


def test_tools_call_unknown_tool_is_protocol_error():
    resp = _call(
        json.dumps(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "nope", "arguments": {}}}
        )
    )
    assert resp["error"]["code"] == INVALID_PARAMS


def test_tools_call_text_stats():
    result = _call_ok("tools/call", {"name": "text_stats", "arguments": {"text": "你好 世界\nsecond line"}})
    payload = json.loads(result["content"][0]["text"])
    assert payload["lines"] == 2
    assert payload["words"] == 4


# -- RegistryToolSource 桥接 ---------------------------------------------------

@pytest.fixture()
def _bridge_tools():
    """向声明式 registry 注册两个测试 spec（L0 + L1），用后清理。"""
    from agentix.tools.definitions import registry

    registry.register(
        ToolSpec(
            name="mcp_bridge_read",
            description="L0 read-only test tool",
            parameters={"type": "object", "properties": {"q": {"type": "string"}}},
            risk_level="L0",
        ),
        lambda: None,
    )
    registry.register(
        ToolSpec(
            name="mcp_bridge_write",
            description="L1 preview tool (must NOT be exported)",
            risk_level="L1",
        ),
        lambda: None,
    )
    yield registry
    registry.delete_tool("mcp_bridge_read")
    registry.delete_tool("mcp_bridge_write")


def _sync_read_handler(args: dict) -> dict:
    return {"echo": args.get("q", "")}


def test_registry_source_exports_only_l0(_bridge_tools):
    executor = ToolExecutor()
    executor.register_handler("mcp_bridge_read", _sync_read_handler)
    source = RegistryToolSource(executor)
    names = {t["name"] for t in source.list_tools()}
    assert "mcp_bridge_read" in names
    assert "mcp_bridge_write" not in names  # L1 不出 MCP 传输层


def test_registry_source_allowlist_and_call(_bridge_tools):
    executor = ToolExecutor()
    executor.register_handler("mcp_bridge_read", _sync_read_handler)
    source = RegistryToolSource(executor, allow=["mcp_bridge_read"])

    out = asyncio.run(source.call_tool("mcp_bridge_read", {"q": "hi"}))
    assert out["data"] == {"echo": "hi"}


def test_registry_source_unknown_tool_maps_to_error(_bridge_tools):
    executor = ToolExecutor()
    source = RegistryToolSource(executor)
    with pytest.raises(Exception) as ei:
        asyncio.run(source.call_tool("mcp_bridge_read", {}))
    assert "unknown tool" in str(ei.value)
