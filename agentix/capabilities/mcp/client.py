"""MCP stdio 客户端 + ToolExecutor 适配器（Tool Runtime 的 MCP Client 分支）。

职责：
- :class:`MCPStdioClient`——spawn MCP server 子进程（``python -m
  agentix.capabilities.mcp.server`` 或任意第三方 server 命令），行分帧 JSON-RPC
  通信：``initialize`` 握手 → ``tools/list`` → ``tools/call``；
- :meth:`MCPStdioClient.register_into`——把远端工具注册进统一
  :class:`ToolExecutor`，Agent 即可像调用本地工具一样调用 MCP 工具
  （继续享受超时/风险/权限治理，MCP 只是传输层）。

安全边界（**2026-09 修订**）：远端工具的来源与能力**未经审核**，因此按
**默认不信任**建模——未显式声明的工具取 ``default_risk_level="L2"`` 且
``side_effect=True``（"不知道有没有副作用"按"有"算）。已审核过的工具须通过
``risk_level_by_tool={"search": "L0"}`` 显式降级。默认加 ``name_prefix="mcp_"``
防与本地工具撞名。

> 旧版（v1）此处写的是"远端工具一律按 L0 只读建模"。那个口径的漏洞是：
> 一个名为 ``admin_reset`` 的远端工具匹配不上任何名称风险模式，
> 会以"可信只读"身份被完全放行。详见 :meth:`register_into` 的说明。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from agentix.config import settings
from agentix.tools.definitions import ToolSpec
from agentix.tools.gateway.executor import ToolExecutor
from agentix.capabilities.mcp.protocol import (
    INTERNAL_ERROR,
    JSONRPC_VERSION,
    MCPError,
)

__all__ = ["MCPStdioClient", "MCPTimeoutError", "scrub_subprocess_env"]

DEFAULT_REQUEST_TIMEOUT_S = 15.0

#: G2：MCP server 是**任意第三方命令**，默认继承父进程全环境等于把 LLM/SERPAPI/DB 密钥
#: 交给它。这里只保留「起进程 + 找到本包 + 正确编码」所需的最小集合。
OS_ENV_ALLOWLIST = frozenset(
    {
        # 进程与解释器本身
        "PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
        "PYTHONPATH", "PYTHONHOME", "PYTHONIOENCODING", "PYTHONUTF8", "VIRTUAL_ENV",
        # POSIX 运行时
        "HOME", "USER", "SHELL", "LANG", "LC_ALL", "TZ", "TERM", "LD_LIBRARY_PATH",
        # 证书与代理（TLS 出网必需，但不含凭据）
        "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    }
)


def scrub_subprocess_env(
    source: Optional[Mapping[str, str]] = None,
    *,
    project_root: Optional[str] = None,
) -> Dict[str, str]:
    """把要交给子进程的环境裁到白名单；白名单外的键（含一切密钥）不进子进程。"""
    base = dict(os.environ if source is None else source)
    extra = {
        key.strip().upper()
        for key in (settings.MCP_SUBPROCESS_ENV_EXTRA_KEYS or "").split(",")
        if key.strip()
    }
    keep = OS_ENV_ALLOWLIST | extra
    scrubbed = {k: v for k, v in base.items() if k.upper() in keep}
    if "PATH" not in scrubbed and "PATH" in base:
        scrubbed["PATH"] = base["PATH"]
    # 白名单裁掉 PYTHONPATH 后 `python -m agentix...` 会找不到包，显式补回仓库根。
    if "PYTHONPATH" not in scrubbed:
        root = project_root or str(Path(agentix_package_root()).parent)
        scrubbed["PYTHONPATH"] = root
    return scrubbed


def agentix_package_root() -> str:
    return str(Path(__file__).resolve().parents[2])


class MCPTimeoutError(MCPError):
    def __init__(self, method: str, timeout_s: float) -> None:
        super().__init__(INTERNAL_ERROR, f"mcp request timeout after {timeout_s}s: {method}")


class MCPStdioClient:
    """与一个 MCP server 子进程的会话（async 上下文管理器）。"""

    def __init__(
        self,
        command: Optional[List[str]] = None,
        *,
        cwd: Optional[str] = None,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    ) -> None:
        # 缺省拉起本仓库自带 server（零依赖，任何装了本包的解释器都能跑）
        self._command = command or [sys.executable, "-m", "agentix.capabilities.mcp.server"]
        self._cwd = cwd
        self._timeout = float(request_timeout_s)
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._next_id = 0
        self._server_info: Dict[str, Any] = {}
        # 串行化 JSON-RPC 请求：两个协程并发 _request 会各自 readline 同一
        # stdout，响应可能被对方读走并因 id 不匹配被丢弃 → 挂到超时/错配结果。
        self._request_lock = asyncio.Lock()

    # -- 生命周期 -----------------------------------------------------------

    async def start(self) -> Dict[str, Any]:
        """启动子进程并完成 initialize 握手，返回 serverInfo。"""
        if self._proc is not None:
            raise RuntimeError("client already started")
        child_env: Optional[Dict[str, str]] = None
        if settings.MCP_SUBPROCESS_ENV_SCRUB:
            child_env = scrub_subprocess_env(project_root=self._cwd)
        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self._cwd,
            env=child_env,
        )
        result = await self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "agentix-mcp-client", "version": "0.1.0"},
            },
        )
        self._server_info = result
        # MCP 规范：握手完成后客户端须回 notifications/initialized
        await self._notify("notifications/initialized", {})
        return result

    async def aclose(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    async def __aenter__(self) -> "MCPStdioClient":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # -- MCP 方法 -----------------------------------------------------------

    @property
    def server_info(self) -> Dict[str, Any]:
        return self._server_info

    async def list_tools(self) -> List[Dict[str, Any]]:
        result = await self._request("tools/list", {})
        return list(result.get("tools") or [])

    async def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """调用工具，返回 MCP content 载荷（``{content: [...], isError?}``）。

        Phase H · W3：``BULKHEAD_MCP_SIZE=0`` 默认直通；>0 时 stdio 下游
        走 MCP 隔离舱（打满只阻塞 MCP 调用方，不串扰其他下游）。
        """
        from agentix.reliability.bulkhead import bulkhead_slot

        async with bulkhead_slot("mcp"):
            return await self._request(
                "tools/call", {"name": name, "arguments": dict(arguments or {})}
            )

    # -- ToolExecutor 适配 ---------------------------------------------------

    async def register_into(
        self,
        executor: ToolExecutor,
        *,
        name_prefix: str = "mcp_",
        timeout_ms: int = 30_000,
        default_risk_level: str = "L2",
        risk_level_by_tool: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        """把远端工具注册进统一执行器（前缀防撞名 + **保守风险建模**）。

        返回注册成功的工具名列表（含前缀）。远端调用失败会以
        ``ToolResult(success=False)`` 落账，不会抛出打断 Agent 主流程。

        **为什么默认按 L2（需人工确认）建模，而不是 L0 只读**：

        远端 MCP 工具的来源、实现与副作用**在协议层完全不可知**——
        ``tools/list`` 只给 ``name`` / ``description`` / ``inputSchema``，
        没有任何可信的能力声明。此前一律硬编码 ``risk_level="L0"``，等于
        「把未知的外部能力声明为只读」，后果是：

        - 声明级闸门（``declared_level_for``）看到 ``LOW``，直接放行；
        - 唯一还能拦的是 ``human_review`` 的**名称模式**兜底
          （``drop|delete|purge|…``），而名称模式是启发式、可被规避的：
          一个叫 ``admin_reset`` 的远端工具两边都不命中 →
          **声明 LOW + 名称 LOW = 执行层完全放行**；
        - ``side_effect=False`` 同理，是一句无从验证的断言。

        现在的契约是**默认不信任**：未在 ``risk_level_by_tool`` 里显式声明
        安全的远端工具一律按 ``L2`` 建模（需人工确认），且
        ``side_effect=True``（"不知道有没有副作用"按"有"算）。已审核过的工具
        请显式传 ``risk_level_by_tool={"search": "L0"}``。

        :param default_risk_level: 未显式声明时的风险等级（默认 ``L2``）。
            传 ``"L0"`` 等于声明"所有远端工具都是只读的"——只有确实审核过
            全套远端工具时才这么做。
        :param risk_level_by_tool: 远端工具名（**不带前缀**）→ 风险等级。
        """
        overrides = dict(risk_level_by_tool or {})
        registered: List[str] = []
        for tool in await self.list_tools():
            remote_name = tool.get("name")
            if not remote_name:
                continue
            local_name = f"{name_prefix}{remote_name}"
            level = overrides.get(remote_name, default_risk_level)
            trusted_read_only = level == "L0"
            spec = ToolSpec(
                name=local_name,
                description=f"[MCP] {tool.get('description', '')}",
                parameters=tool.get("inputSchema") or {"type": "object", "properties": {}},
                risk_level=level,
                # 未显式声明为只读 → 不知道有没有副作用，按"有"算
                side_effect=not trusted_read_only,
                control=(
                    ""
                    if trusted_read_only
                    else (
                        "MCP 远端工具的来源与能力默认未经审核：按「需人工确认（HITL）」"
                        "建模，未获批准不得执行；审核后可经 risk_level_by_tool 降为 L0。"
                    )
                ),
                retryable=False,
                timeout_ms=timeout_ms,
            )

            async def _handler(args: Dict[str, Any], *, _remote: str = remote_name) -> Any:
                payload = await self.call_tool(_remote, args)
                if payload.get("isError"):
                    texts = [c.get("text", "") for c in payload.get("content", []) if isinstance(c, dict)]
                    raise RuntimeError("; ".join(t for t in texts if t) or "mcp tool error")
                return payload.get("content")

            executor.register_handler(local_name, _handler, spec=spec)
            registered.append(local_name)
        return registered

    # -- JSON-RPC 传输 -------------------------------------------------------

    async def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("client not started: await start() first")
        # 同一时刻只允许一个 in-flight 请求（见 _request_lock 注释）
        async with self._request_lock:
            if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
                raise RuntimeError("client not started: await start() first")
            self._next_id += 1
            req = {"jsonrpc": JSONRPC_VERSION, "id": self._next_id, "method": method, "params": params}
            self._proc.stdin.write((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
            await self._proc.stdin.drain()

            request_id = self._next_id
            while True:
                try:
                    line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=self._timeout)
                except asyncio.TimeoutError as exc:
                    raise MCPTimeoutError(method, self._timeout) from exc
                if not line:
                    raise MCPError(INTERNAL_ERROR, f"mcp server closed stream during {method}")
                response = self._parse_line(line, method)
                if response is None:
                    continue  # 通知/噪声行，继续读
                if response.get("id") != request_id:
                    continue  # 过期/噪声响应，防御性跳过
                if "error" in response:
                    err = response["error"] or {}
                    raise MCPError(
                        int(err.get("code", INTERNAL_ERROR)),
                        str(err.get("message", "unknown mcp error")),
                        err.get("data"),
                    )
                result = response.get("result")
                return result if isinstance(result, dict) else {}

    async def _notify(self, method: str, params: Dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("client not started: await start() first")
        req = {"jsonrpc": JSONRPC_VERSION, "method": method, "params": params}
        self._proc.stdin.write((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    def _parse_line(self, line: bytes, method: str) -> Optional[Dict[str, Any]]:
        try:
            msg = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, ValueError):
            return None  # 噪声行（如 server 误写 stderr 内容到 stdout），跳过
        if isinstance(msg, dict) and "id" in msg:
            return msg
        return None
