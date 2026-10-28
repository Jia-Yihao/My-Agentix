"""MCP stdio 客户端 + ToolExecutor 适配器（Tool Runtime 的 MCP Client 分支）。

职责：
- :class:`MCPStdioClient`——spawn MCP server 子进程（``python -m
  agentix.capabilities.mcp.server`` 或任意第三方 server 命令），行分帧 JSON-RPC
  通信：``initialize`` 握手 → ``tools/list`` → ``tools/call``；
- :meth:`MCPStdioClient.register_into`——把远端工具注册进统一
  :class:`ToolExecutor`，Agent 即可像调用本地工具一样调用 MCP 工具
  （继续享受超时/风险/权限治理，MCP 只是传输层）；
- **T25 断连自愈**：子进程异常死亡（传输断流/管道损坏）后按**有界重连**恢复
  （计次上限 + 指数退避 + 日志留痕）；耗尽后进入降级冷却窗口，调用方拿到
  可判读的 :class:`MCPReconnectExhaustedError` 而非无解释的挂起（口径见
  docs/design/审批与中断恢复优化规划.md T25 卡）。

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
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from agentix.config import settings
from agentix.domain.tool.exec_env import OS_ENV_ALLOWLIST as _SHARED_OS_ENV_ALLOWLIST
from agentix.domain.tool.exec_env import scrub_env
from agentix.tools.definitions import ToolSpec
from agentix.tools.gateway.executor import ToolExecutor
from agentix.capabilities.mcp.protocol import (
    INTERNAL_ERROR,
    JSONRPC_VERSION,
    MCPError,
)

__all__ = [
    "MCPStdioClient",
    "MCPTimeoutError",
    "MCPTransportGoneError",
    "MCPReconnectExhaustedError",
    "scrub_subprocess_env",
]

logger = logging.getLogger(__name__)

DEFAULT_REQUEST_TIMEOUT_S = 15.0

# T25 有界重连常量。**刻意不进 Settings**：本批次 `agentix/config/settings.py`
# 被另一任务独占（禁新增配置键，避免撞车）；若后续解禁，这三个常量应迁往
# Settings 并按纪律 4 五处同步——迁移前以本处为唯一真源。
#: 单次断连触发的重连 spawn 尝试上限（防重连风暴：进程起停循环必须有封顶）。
MCP_RECONNECT_MAX_ATTEMPTS = 3
#: 指数退避基数（秒）：第 n 次重连尝试前 sleep ``base * 2**(n-1)``（0.5 / 1.0 / 2.0）。
MCP_RECONNECT_BACKOFF_BASE_S = 0.5
#: 耗尽后的降级冷却窗口（秒）：窗口内新调用**即刻**拿到明示降级拒绝——既不反复
#: spawn 放大风暴，也不把"辅助子系统故障"变成新的挂起或主链拒绝路径（纪律 2）。
MCP_RECONNECT_DEGRADED_COOLDOWN_S = 30.0

#: G2 的白名单判据**只有一个真源**：`agentix.domain.tool.exec_env.OS_ENV_ALLOWLIST`。
#: 这里保留旧名字做再导出（`tests/test_p107_sandbox_hardening.py` 从本模块取符号），
#: 因为 §24.3 T2 ⑮ 的 `subprocess` 沙箱要裁的是同一件事——两处各抄一份白名单，
#: 迟早只关一条（AGENTS.md §15.9）。
OS_ENV_ALLOWLIST = _SHARED_OS_ENV_ALLOWLIST


def scrub_subprocess_env(
    source: Optional[Mapping[str, str]] = None,
    *,
    project_root: Optional[str] = None,
) -> Dict[str, str]:
    """把要交给子进程的环境裁到白名单；白名单外的键（含一切密钥）不进子进程。

    判据本体在域层（`exec_env.scrub_env`）；本函数只补 MCP 特有的两件事：
    读 ``MCP_SUBPROCESS_ENV_EXTRA_KEYS``，以及 ``project_root`` 缺省时回落本包仓库根。
    """
    return scrub_env(
        source,
        extra_keys=settings.MCP_SUBPROCESS_ENV_EXTRA_KEYS,
        project_root=project_root or str(Path(agentix_package_root()).parent),
    )


def agentix_package_root() -> str:
    return str(Path(__file__).resolve().parents[2])


class MCPTimeoutError(MCPError):
    def __init__(self, method: str, timeout_s: float) -> None:
        super().__init__(INTERNAL_ERROR, f"mcp request timeout after {timeout_s}s: {method}")


class MCPTransportGoneError(MCPError):
    """传输层断流（子进程死亡/管道损坏）——有界重连的**唯一**触发信号。

    做成 :class:`MCPError` 子类：既有按 ``MCPError`` 兜底捕获的调用方语义不变；
    自愈只是在错误之下多了一层尝试，不是错误类型变更。
    """

    def __init__(self, method: str, reason: str) -> None:
        super().__init__(INTERNAL_ERROR, f"mcp server closed stream during {method}: {reason}")


class MCPReconnectExhaustedError(MCPError):
    """有界重连耗尽后的**明示降级**信号（验收③）：失败原因可判读、调用不挂起。

    ``data`` 携带 ``degraded``/``reconnect_attempts`` 供上层结构化判读；
    消息本体含 "reconnect exhausted" 或 "degraded window"（冷却窗口内的快速拒绝）。
    """

    def __init__(self, attempts: int, last_reason: str, cooldown_s: float) -> None:
        super().__init__(
            INTERNAL_ERROR,
            f"mcp subprocess reconnect exhausted after {attempts} attempts; "
            f"tool surface degraded for {cooldown_s:.1f}s: {last_reason}",
            data={"degraded": True, "reconnect_attempts": attempts},
        )


class MCPStdioClient:
    """与一个 MCP server 子进程的会话（async 上下文管理器）。"""

    def __init__(
        self,
        command: Optional[List[str]] = None,
        *,
        cwd: Optional[str] = None,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        reconnect_max_attempts: Optional[int] = None,
        reconnect_backoff_base_s: Optional[float] = None,
        reconnect_cooldown_s: Optional[float] = None,
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
        # -- T25 有界重连状态（缺省取模块常量；构造参数只为测试/特殊场景可注入，
        #    刻意不新开 Settings 键——见模块常量处的说明） --
        self._reconnect_max_attempts = (
            MCP_RECONNECT_MAX_ATTEMPTS if reconnect_max_attempts is None else int(reconnect_max_attempts)
        )
        self._reconnect_backoff_base_s = (
            MCP_RECONNECT_BACKOFF_BASE_S
            if reconnect_backoff_base_s is None
            else float(reconnect_backoff_base_s)
        )
        self._reconnect_cooldown_s = (
            MCP_RECONNECT_DEGRADED_COOLDOWN_S
            if reconnect_cooldown_s is None
            else float(reconnect_cooldown_s)
        )
        # 曾成功 spawn 过：区分"从未 start"（基线 RuntimeError）与"start 过但传输
        # 已死"（进入有界自愈）。
        self._ever_started = False
        # 用户显式 aclose 过：显式关闭不是"异常断连"，不得触发自愈（保守边，
        # 保持与基线一致——关掉的客户端再调用仍报 not started）。
        self._user_closed = False
        # 耗尽后的降级窗口截止时刻（loop.time() 基准）；None = 未处于降级。
        self._degraded_until: Optional[float] = None
        # 退避 sleep 的可注入点：测试断言退避时序而不真等；生产恒为 asyncio.sleep。
        self._sleep = asyncio.sleep

    # -- 生命周期 -----------------------------------------------------------

    async def start(self) -> Dict[str, Any]:
        """启动子进程并完成 initialize 握手，返回 serverInfo。"""
        if self._proc is not None:
            raise RuntimeError("client already started")
        # 显式 start 撤销"用户已关闭"标记：重启是明确意图，重连守卫随之复位。
        self._user_closed = False
        # 同一理由适用于降级冷却窗口：显式 start 是明确意图——调用方修复根因后重启，
        # 握手成功（会话明确健康）后的第一次调用不得再被上一轮的窗口误拒为
        # "degraded window active"，否则就是与事实相反的降级声明。
        self._degraded_until = None
        return await self._spawn_and_handshake()

    async def _spawn_and_handshake(self) -> Dict[str, Any]:
        """拉起子进程 + initialize 握手（start 与重连自愈共用的原子步骤）。"""
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
        self._ever_started = True
        result = await self._request_raw(
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
        self._user_closed = True
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
            if not self._ever_started or self._user_closed:
                # 基线语义保留：从未 start（或用户显式关过）的调用不是"异常断连"，
                # 不触发自愈，照旧快速失败。
                raise RuntimeError("client not started: await start() first")
        async with self._request_lock:
            return await self._request_with_reconnect(method, params)

    async def _request_with_reconnect(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """重连循环的**退出收尾包装**（T25 返工严重①）：任何非成功退出都回收本次子进程。

        为什么必须有这层：工具级超时（bulkhead 的 ``asyncio.wait_for``，
        agentix/reliability/bulkhead.py:97-106）会在下一个 await 点把协程
        ``CancelledError`` 掉，而下面的循环体只捕 ``MCPError/OSError/RuntimeError``
        与 ``MCPTransportGoneError``——取消直接外抛。若不顺手回收：① ``_proc``
        指向的半初始化孤儿子进程泄漏；② 下一次调用因 ``_proc`` 非空走 step 0
        直发，只会拿到超时/断流错误，**自愈能力此后永久失效**（跨请求风暴守卫
        也从未生效）。回收后 ``_proc = None``，下一次调用重新进入 step 1 重连。

        刻意**不**为取消路径武装降级窗口：取消是调用方侧的预算裁决，不是"服务
        端已死"的证据；风暴守卫的语义是"每次触发 spawn 计数封顶"，取消后的重新
        自愈仍受同一上限约束，不存在无界风暴。
        """
        try:
            return await self._reconnect_loop(method, params)
        except BaseException:  # noqa: BLE001 - 必须含 CancelledError（BaseException 派生）在内的所有外抛路径回收子进程；动作只有回收+原样重抛，异常不被吞、失败仍能被观察到
            self._reap_zombie()
            raise

    async def _reconnect_loop(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """持锁下的有界重连发送循环（T25 验收①②③的执行体）。

        预算：一次触发最多 ``_reconnect_max_attempts`` 次重连 spawn，每次 spawn 前
        指数退避 ``base * 2**(n-1)``；spawn 尝试次数封顶即"计次上限防风暴"。
        超时不在此列（保守边：挂住 ≠ 死亡，本轮不重连，仍走 MCPTimeoutError）。
        """
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            if not self._ever_started or self._user_closed:
                raise RuntimeError("client not started: await start() first")

        # 降级冷却窗口：耗尽窗口内即刻拒绝——不重复 spawn（窗口本身就是风暴守卫的
        # 跨请求形态），也不进入任何等待路径（验收③：不吃无解释的挂起）。
        if self._degraded_until is not None:
            remaining = self._degraded_until - asyncio.get_running_loop().time()
            if remaining > 0:
                raise MCPReconnectExhaustedError(
                    self._reconnect_max_attempts,
                    f"degraded window active ({remaining:.1f}s remaining); "
                    f"reconnect suppressed to avoid reconnect storm",
                    self._reconnect_cooldown_s,
                )
            self._degraded_until = None  # 窗口已过：允许新一轮有界自愈

        last_reason = "mcp subprocess not alive"
        # step 0 = 直发（传输可能还活着）；step n≥1 = 先退避+重连再直发。
        # 总 spawn 尝试 ≤ max_attempts，封顶。
        for step in range(self._reconnect_max_attempts + 1):
            if step:
                wait_s = self._reconnect_backoff_base_s * (2 ** (step - 1))
                logger.warning(
                    "mcp reconnecting after transport loss (during %s): attempt %d/%d, backoff %.3fs; last error: %s",
                    method, step, self._reconnect_max_attempts, wait_s, last_reason,
                )
                await self._sleep(wait_s)
                try:
                    await self._spawn_and_handshake()
                except (MCPError, OSError, RuntimeError) as exc:
                    # 重连尝试失败属于自愈循环内部：记录原因、回收残骸、进入下一次
                    # 退避——不向上抛，否则"子进程单次死亡"被放大成主链异常（纪律 2）。
                    last_reason = f"{type(exc).__name__}: {exc}"
                    self._reap_zombie()
                    continue
                logger.info(
                    "mcp subprocess reconnect succeeded: attempt %d/%d (during %s)",
                    step, self._reconnect_max_attempts, method,
                )
            if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
                continue  # 传输仍不可用：进入下一轮退避重连
            try:
                return await self._request_raw(method, params)
            except MCPTransportGoneError as exc:
                last_reason = f"{type(exc).__name__}: {exc}"
                self._reap_zombie()
                continue

        # 耗尽：进入降级窗口 + ERROR 留痕 + 可判读的失败原因（不假装成功）。
        self._degraded_until = asyncio.get_running_loop().time() + self._reconnect_cooldown_s
        logger.error(
            "mcp subprocess reconnect exhausted after %d attempts (during %s): "
            "tool surface degraded for %.1fs; last error: %s",
            self._reconnect_max_attempts, method, self._reconnect_cooldown_s, last_reason,
        )
        raise MCPReconnectExhaustedError(self._reconnect_max_attempts, last_reason, self._reconnect_cooldown_s)

    def _reap_zombie(self) -> None:
        """尽力清理已断连的子进程句柄（必须在请求锁内调用）。

        断连场景下进程通常已自行退出（stdout EOF 即其信号）；kill/wait 只是兜底
        残骸，失败没有补救对象——真实错误原因已由调用方记录（纪律 2：辅助回收
        不得制造新故障点）。
        """
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except Exception:  # noqa: BLE001 - 管道随进程死亡，关闭失败无动作可做；回收尽力而为，不影响已记录的主因
                pass
        if proc.returncode is None:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001 - 进程可能恰在此刻退出；杀不杀得掉都不改变"已断连"事实
                pass

    async def _request_raw(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """单次发送并等待响应；不做任何重连（供握手与自愈循环内部复用）。"""
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("client not started: await start() first")
        self._next_id += 1
        req = {"jsonrpc": JSONRPC_VERSION, "id": self._next_id, "method": method, "params": params}
        try:
            self._proc.stdin.write((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
            await self._proc.stdin.drain()
        except OSError as exc:
            # 管道写失败（BrokenPipeError/ConnectionResetError 等）= 子进程已死：
            # 归入传输断流，交给有界重连，而不是各抛各的。
            raise MCPTransportGoneError(method, f"stdin write failed: {exc}") from exc

        request_id = self._next_id
        while True:
            try:
                line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=self._timeout)
            except asyncio.TimeoutError as exc:
                raise MCPTimeoutError(method, self._timeout) from exc
            if not line:
                raise MCPTransportGoneError(method, "stdout closed by server")
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
