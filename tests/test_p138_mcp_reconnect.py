"""P138：MCP 子进程断连的有界重连自愈（T25）。

覆盖（验收标准①–③）：
- ① 子进程异常后有界重连：单次触发的 spawn 尝试上限 = reconnect_max_attempts
  （模块常量 MCP_RECONNECT_MAX_ATTEMPTS=3），指数退避 base*2**(n-1)（默认 0.5s 起步，
  即 0.5 / 1.0 / 2.0），用例以假 sleep 记录时序，不做真实等待；
- ② 重连成功（INFO）与耗尽（ERROR）均可观测（日志留痕）；
- ③ 耗尽后明示降级：MCPReconnectExhaustedError（data.degraded=True）+ 冷却窗口内
  即刻拒绝且不再反复 spawn（防重连风暴）、调用不挂起；handler 异常经 ToolExecutor
  统一落 ToolResult(success=False)，不抛穿主链（约定 2）；
- 不变语义保留：超时（MCPTimeoutError）不触发重连（保守边）；未 start 的调用仍抛
  RuntimeError("client not started")。

第 2 轮返工（reviewer 严重①②）追加覆盖：
- 严重①：取消路径（工具级超时 wait_for 的 CancelledError）落自愈循环中途时，
  退出收尾必须回收本次 spawn 的子进程并清空 _proc（否则进程泄漏 + 自愈永久失效）；
- 严重②：耗尽后显式 start() 复位降级窗口——修复根因重启后的首次调用不得被
  "degraded window active" 误拒。

所有用例用伪造子进程桩驱动，不依赖真实 MCP server 或网络；spawner 计数直接证明
"重连风暴不会发生"。
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from agentix.capabilities.mcp.client import (
    MCPReconnectExhaustedError,
    MCPStdioClient,
    MCPTimeoutError,
    MCPTransportGoneError,
)
from agentix.tools.definitions import registry as tool_registry
from agentix.tools.gateway.executor import ToolExecutor

pytestmark = pytest.mark.asyncio

LOGGER_NAME = "agentix.capabilities.mcp.client"


# ---------------------------------------------------------------------------
# 桩：最小脚本化 JSON-RPC server（不出网、不起真子进程）
# ---------------------------------------------------------------------------


class _ScriptedServer:
    """按开关模拟子进程行为：握手可用/瞬死、tools/call 应答/断流。"""

    def __init__(
        self,
        *,
        die_after_init: bool = False,
        die_on_call: bool = False,
        never_respond: bool = False,
        die_after_n_calls: int | None = None,
    ) -> None:
        self.die_after_init = die_after_init
        self.die_on_call = die_on_call
        self.never_respond = never_respond
        # 前 n 次 tools/call 正常应答、第 n+1 次起断流：模拟"跑着跑着进程死了"
        self.die_after_n_calls = die_after_n_calls
        self.call_count = 0
        self.dead = False
        self.outbox: list[str] = []

    def on_write(self, raw: str) -> None:
        msg = json.loads(raw)
        if "id" not in msg:  # 通知：不需要响应
            return
        method = msg.get("method")
        rid = msg["id"]
        if method == "initialize":
            if self.die_after_init:
                # 拉起即死：不写响应，readline 将拿到 EOF
                self.dead = True
                return
            self.outbox.append(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {"serverInfo": {"name": "fake-mcp-server"}},
                    }
                )
            )
        elif method == "tools/list":
            self.outbox.append(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "tools": [
                                {
                                    "name": "fake_tool",
                                    "description": "fake",
                                    "inputSchema": {"type": "object", "properties": {}},
                                }
                            ]
                        },
                    }
                )
            )
        elif method == "tools/call":
            self.call_count += 1
            if self.die_after_n_calls is not None and self.call_count > self.die_after_n_calls:
                self.dead = True
            if self.die_on_call or self.never_respond or self.dead:
                self.dead = not self.never_respond  # never_respond 是挂住，不是断流
                return
            self.outbox.append(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {"content": [{"type": "text", "text": "ok-from-fake"}]},
                    }
                )
            )
        else:
            self.outbox.append(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {}}))


class _FakeStdin:
    def __init__(self, server: _ScriptedServer) -> None:
        self._server = server

    def write(self, data: bytes) -> None:
        self._server.on_write(data.decode("utf-8"))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeStdout:
    def __init__(self, server: _ScriptedServer) -> None:
        self._server = server

    async def readline(self) -> bytes:
        if self._server.never_respond:
            # 挂住不回：等待父侧超时截断（模拟进程活着但卡死）
            await asyncio.sleep(30)
            return b""
        if self._server.dead or not self._server.outbox:
            return b""  # EOF：子进程退出、管道关闭的真实形态
        line = self._server.outbox.pop(0)
        return (line + "\n").encode("utf-8")


class _FakeProc:
    def __init__(self, server: _ScriptedServer) -> None:
        self.stdin = _FakeStdin(server)
        self.stdout = _FakeStdout(server)
        self.returncode: int | None = None

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return 0 if self.returncode is None else self.returncode


class _Spawner:
    """create_subprocess_exec 的桩：计数 spawn 次数，按队列发放脚本化 server。

    队列耗尽后抛 FileNotFoundError——与真实 spawn 失败的形态一致，用于把重连
    次数钉死在上限上（计数即证明无风暴）。
    """

    def __init__(self, servers: list[_ScriptedServer]) -> None:
        self._servers = list(servers)
        self.count = 0
        # 记录发出去的每个假进程：取消路径用例要断言"被取消的那只已被 kill()"
        self.procs: list[_FakeProc] = []

    async def __call__(self, *cmd, **kwargs) -> _FakeProc:
        self.count += 1
        if not self._servers:
            raise FileNotFoundError("no fake server left in stub queue")
        proc = _FakeProc(self._servers.pop(0))
        self.procs.append(proc)
        return proc


def _build_client(
    monkeypatch: pytest.MonkeyPatch,
    servers: list[_ScriptedServer],
    *,
    max_attempts: int = 3,
    backoff_base_s: float = 0.5,
    cooldown_s: float = 30.0,
    request_timeout_s: float = 1.0,
) -> tuple[MCPStdioClient, _Spawner, list[float]]:
    client = MCPStdioClient(
        ["fake", "mcp", "cmd"],
        request_timeout_s=request_timeout_s,
        reconnect_max_attempts=max_attempts,
        reconnect_backoff_base_s=backoff_base_s,
        reconnect_cooldown_s=cooldown_s,
    )
    waits: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    client._sleep = _fake_sleep
    spawner = _Spawner(servers)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawner)
    return client, spawner, waits


# ---------------------------------------------------------------------------
# ①：断连触发有界重连；计次上限防风暴；退避时序可判定
# ---------------------------------------------------------------------------


async def test_transport_death_triggers_bounded_reconnect_and_recovers(monkeypatch):
    """子进程在调用中死亡 ⇒ 有界重连；首个替代 server 瞬死、第二个成功 ⇒ 恢复且时序可判定。"""
    srv1 = _ScriptedServer(die_on_call=True)
    srv_bad = _ScriptedServer(die_after_init=True)  # 第一次重连：拉起即死
    srv_good = _ScriptedServer()
    client, spawner, waits = _build_client(monkeypatch, [srv1, srv_bad, srv_good])

    await client.start()
    assert spawner.count == 1

    payload = await client.call_tool("fake_tool", {})
    assert payload["content"][0]["text"] == "ok-from-fake"
    # 退避时序：第 1 次重连等 0.5s，第 2 次等 1.0s（base*2**(n-1)），假 sleep 记录、未真等
    assert waits == [0.5, 1.0]
    # 风暴守卫：spawn 总数 = 1（首次）+ 2（重连尝试），远小于"无上限"形态
    assert spawner.count == 3


async def test_reconnect_spawn_attempts_never_exceed_cap(monkeypatch):
    """重连全部失败 ⇒ spawn 尝试恰好等于上限，不多不少（计次证明不会风暴）。"""
    srv1 = _ScriptedServer(die_on_call=True)
    client, spawner, waits = _build_client(
        monkeypatch, [srv1], max_attempts=3, backoff_base_s=0.5
    )

    await client.start()
    with pytest.raises(MCPReconnectExhaustedError):
        await client.call_tool("fake_tool", {})

    assert spawner.count == 1 + 3  # 首次 start + 3 次重连尝试封顶
    assert waits == [0.5, 1.0, 2.0]  # 指数退避口径：base * 2**(n-1)


# ---------------------------------------------------------------------------
# ②：重连成功 / 耗尽均可观测
# ---------------------------------------------------------------------------


async def test_reconnect_success_and_exhaustion_observable(monkeypatch, caplog):
    """成功路径 INFO 留痕、耗尽路径 ERROR 留痕，都可被日志观测到。"""
    srv1 = _ScriptedServer(die_on_call=True)
    srv_good = _ScriptedServer()
    client_ok, _, _ = _build_client(monkeypatch, [srv1, srv_good])
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        await client_ok.start()
        await client_ok.call_tool("fake_tool", {})
    assert any(
        r.levelno == logging.INFO and "reconnect succeeded" in r.getMessage()
        for r in caplog.records
    ), "验收②：重连成功必须可观测"

    caplog.clear()
    srv2 = _ScriptedServer(die_on_call=True)
    client_ex, _, _ = _build_client(monkeypatch, [srv2], max_attempts=2)
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        with pytest.raises(MCPReconnectExhaustedError):
            await client_ex.start()
            await client_ex.call_tool("fake_tool", {})
    assert any(
        r.levelno == logging.ERROR and "reconnect exhausted" in r.getMessage()
        for r in caplog.records
    ), "验收②：耗尽必须可观测"


# ---------------------------------------------------------------------------
# ③：耗尽后明示降级、调用不挂起、冷却窗口不放大风暴
# ---------------------------------------------------------------------------


async def test_exhaustion_raises_readable_degraded_reason(monkeypatch):
    """耗尽后失败原因可判读（data.degraded=True + 消息含 exhausted），不静默、不假装成功。"""
    srv1 = _ScriptedServer(die_on_call=True)
    client, spawner, _ = _build_client(monkeypatch, [srv1], max_attempts=2)
    await client.start()
    with pytest.raises(MCPReconnectExhaustedError) as excinfo:
        await client.call_tool("fake_tool", {})
    assert "reconnect exhausted" in str(excinfo.value)
    assert excinfo.value.data == {"degraded": True, "reconnect_attempts": 2}


async def test_degraded_cooldown_window_rejects_fast_without_new_spawns(monkeypatch):
    """耗尽后的冷却窗口：后续调用即刻拿到明示降级拒绝，不再反复 spawn（防风暴、不吃挂起）。"""
    srv1 = _ScriptedServer(die_on_call=True)
    client, spawner, waits = _build_client(
        monkeypatch, [srv1], max_attempts=2, backoff_base_s=0.5, cooldown_s=30.0
    )
    await client.start()
    with pytest.raises(MCPReconnectExhaustedError):
        await client.call_tool("fake_tool", {})
    spawns_after_exhaustion = spawner.count
    waits_after_exhaustion = list(waits)

    # 冷却窗口内的第二次调用：直接拒绝，不新增 spawn、不新增退避等待（不挂起）
    with pytest.raises(MCPReconnectExhaustedError) as excinfo:
        await client.call_tool("fake_tool", {})
    assert "degraded window" in str(excinfo.value)
    assert spawner.count == spawns_after_exhaustion
    assert waits == waits_after_exhaustion


async def test_exhausted_mcp_error_does_not_break_main_chain(monkeypatch):
    """约定 2：耗尽的 MCP 故障经 ToolExecutor 统一落 ToolResult(success=False)，不抛穿主链。"""
    # 第一次调用正常应答，之后进程死亡；重连上限 1 次且桩队列已空 ⇒ 快速耗尽
    srv1 = _ScriptedServer(die_after_n_calls=1)
    client, _, _ = _build_client(monkeypatch, [srv1], max_attempts=1)
    executor = ToolExecutor()
    await client.start()
    registered = await client.register_into(
        executor, risk_level_by_tool={"fake_tool": "L0"}
    )
    try:
        assert "mcp_fake_tool" in registered
        # 先经正常调用建立会话，再让桩进程在调用中死亡触发自愈
        ok = await client.call_tool("fake_tool", {})
        assert ok["content"][0]["text"] == "ok-from-fake"
        result = await executor.execute("mcp_fake_tool", {})
        assert result.success is False
        assert "reconnect exhausted" in (result.error or "")
        result = await executor.execute("mcp_fake_tool", {})
        assert result.success is False
        assert "reconnect exhausted" in (result.error or "")
    finally:
        await client.aclose()
        for name in list(registered):
            tool_registry.delete_tool(name)


# ---------------------------------------------------------------------------
# 不变语义保留（保守边）
# ---------------------------------------------------------------------------


async def test_timeout_does_not_trigger_reconnect(monkeypatch):
    """超时仍走 MCPTimeoutError（保守边：本轮只处理进程死亡/断流，不重连挂住场景）。"""
    srv = _ScriptedServer(never_respond=True)
    client, spawner, _ = _build_client(monkeypatch, [srv], request_timeout_s=0.05)
    with pytest.raises(MCPTimeoutError):
        await client.start()
    await client.aclose()
    assert spawner.count == 1  # 未触发重连


async def test_transport_gone_error_remains_mcp_error_subclass(monkeypatch):
    """兼容守卫：断流错误仍是 MCPError 子类，既有按 MCPError 兜底的调用方语义不变。"""
    from agentix.capabilities.mcp.protocol import MCPError

    assert issubclass(MCPTransportGoneError, MCPError)
    assert issubclass(MCPReconnectExhaustedError, MCPError)


async def test_call_before_start_still_raises_runtime_error(monkeypatch):
    """基线保留：从未 start 的客户端调用仍是 RuntimeError('client not started')。"""
    client = MCPStdioClient(["fake", "cmd"])
    with pytest.raises(RuntimeError, match="client not started"):
        await client.call_tool("whatever", {})


# ---------------------------------------------------------------------------
# 第 2 轮返工：严重①（取消路径退出收尾）/ 严重②（start 复位降级窗口）
# ---------------------------------------------------------------------------


async def test_cancellation_mid_reconnect_reaps_child_and_clears_proc(monkeypatch):
    """严重①：工具级超时取消（CancelledError）落自愈循环中途 ⇒ 必须回收本次子进程。

    模拟 bulkhead ``asyncio.wait_for`` 的取消：call_tool 在重连后的挂住进程上
    等待响应时被掐断。不回收的后果（reviewer 推演）：``_proc`` 停在半初始化
    孤儿进程上——进程泄漏，且下一次调用因 ``_proc`` 非空走 step 0 直发，
    自愈能力永久失效。断言两点：``_proc is None``、被取消的那只假进程
    ``returncode`` 已被 ``kill()`` 置位（-9）；并按设计选择断言取消不武装降级窗口。
    """
    srv1 = _ScriptedServer(die_on_call=True)  # 首调断流 ⇒ 触发自愈
    srv_hang = _ScriptedServer(never_respond=True)  # 重连拉起一只"挂住不回"的进程
    client, spawner, _ = _build_client(
        monkeypatch, [srv1, srv_hang], max_attempts=3, backoff_base_s=0.05, cooldown_s=30.0
    )
    await client.start()

    # 0.6s 处取消（假 sleep 即时返回，取消点落在挂住的 tools/call 读取上）
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(client.call_tool("fake_tool", {}), 0.6)

    assert client._proc is None, "取消退出必须清空 _proc，否则下一次调用停在半初始化会话"
    assert spawner.procs[-1].returncode == -9, "取消退出必须回收本次 spawn 的子进程（_reap_zombie → kill）"
    assert client._degraded_until is None, "取消是调用方侧预算裁决，不是服务端已死的证据，刻意不武装降级窗口"


async def test_explicit_start_after_exhaustion_clears_degraded_window(monkeypatch):
    """严重②：耗尽后调用方修复根因并显式 start() ⇒ 降级窗口必须随之复位。

    不复位的后果（reviewer 推演）：握手成功、会话明确健康后的第一次调用仍被
    上一轮窗口抛 "degraded window active" 误拒——与"显式 start 是明确意图，
    守卫随之复位"的注释口径自相矛盾，且给出与事实相反的降级声明。
    """
    srv1 = _ScriptedServer(die_on_call=True)
    client, spawner, _ = _build_client(monkeypatch, [srv1], max_attempts=1, cooldown_s=30.0)
    await client.start()
    with pytest.raises(MCPReconnectExhaustedError):
        await client.call_tool("fake_tool", {})
    assert client._degraded_until is not None, "前提：耗尽后窗口已武装"

    # 模拟"根因已修复"：桩队列放入健康 server，显式重启后立即调用（窗口远未过期）
    spawner._servers.append(_ScriptedServer())
    await client.start()
    payload = await client.call_tool("fake_tool", {})
    assert payload["content"][0]["text"] == "ok-from-fake", "重启后的首次调用不得被旧窗口误拒"
    assert client._degraded_until is None
