"""Phase 6 — Capabilities MCP（原 agentix.mcp）。"""
from agentix.capabilities.mcp.protocol import PROTOCOL_VERSION
from agentix.capabilities.mcp.client import MCPStdioClient, MCPTimeoutError
from agentix.capabilities.mcp.server import MCPServer
from agentix.capabilities.mcp.tool_source import (
    OfflineToolSource,
    RegistryToolSource,
    StaticToolSource,
)
__all__ = [
    "PROTOCOL_VERSION", "MCPStdioClient", "MCPTimeoutError",
    "MCPServer", "OfflineToolSource", "RegistryToolSource", "StaticToolSource",
]
