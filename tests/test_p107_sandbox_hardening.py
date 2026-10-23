"""P107：执行边界加固（§24.3 T0 落地回归）。

覆盖《PROJECT_SPEC》§24.2 实测缺口 G1/G2/G3/G4/G5/G6/G9/G11 的修复：
- G1 ``TOOL_SYNC_OFFLOAD_ENABLED``——同步 handler 下放线程后 timeout 才真能截断；
- G2 ``MCP_SUBPROCESS_ENV_SCRUB``——MCP 子进程不再继承主进程密钥；
- G3 内置计算器资源上限——``9**9**9`` 立即抛错，且一元负号不再被吞；
- G4 搜索日志脱敏——httpx 异常文本里的 ``api_key`` 不进日志；
- G5 Text2SQL 单一词法——两闸判定一致、剥字面量后消误杀、补上读文件/打时间模式；
- G6 生产身份显式化——``APP_ENV`` 优先于 DEBUG 推断；
- G9 设置接口密钥掩码——响应体不含明文 key，PUT 只认白名单键；
- G11 ``DB_ECHO``——覆盖 DEBUG 推断出的 echo。

全部离线，零外部依赖；flag-off 与加固前逐位等价的口径也在此断言。
"""

from __future__ import annotations

import asyncio
import time

import pytest

# text2sql 的 components 包内存在既有循环（components/text2sql/__init__ ↔
# application/agents/text2sql/components），必须先经规范入口进来一次，
# 否则单独 import 其子模块会 ImportError。
import agentix.application.agents.text2sql  # noqa: F401
from agentix.config import settings
from agentix.domain.policies.sql_read_only import (
    check_read_only,
    find_dangerous,
    strip_literals,
)


# ---------------------------------------------------------------------------
# G3：内置计算器资源上限
# ---------------------------------------------------------------------------


def _calc(expression: str):
    from agentix.capabilities.mcp.tool_source import _calc as real_calc

    return real_calc({"expression": expression})["value"]


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("1+2*3", 7),
        ("(2+3)**2", 25),
        ("7//2", 3),
        ("10%3", 1),
        ("2**10", 1024),
        ("6*7", 42),
    ],
)
def test_calc_still_evaluates_ordinary_expressions(expression, expected):
    assert _calc(expression) == expected


@pytest.mark.parametrize("expression", ["-5", "-4+2", "-(2+3)"])
def test_calc_unary_minus_is_not_swallowed(expression):
    """加固前 USub 只递归取 operand，``-5`` 会算成 ``5``。"""
    assert _calc(expression) < 0


@pytest.mark.parametrize(
    "expression",
    ["9**9**9", "2**1000", "2**-70", "1e309", "1" + "+1" * 300],
)
def test_calc_rejects_resource_explosion(expression):
    started = time.perf_counter()
    with pytest.raises(ValueError):
        _calc(expression)
    assert time.perf_counter() - started < 0.5


def test_calc_rejects_non_finite_result():
    with pytest.raises(ValueError):
        _calc("1e308 * 1e308 / 1e-308")


# ---------------------------------------------------------------------------
# G4：搜索日志脱敏
# ---------------------------------------------------------------------------


def test_redact_secrets_masks_url_and_header_forms():
    from agentix.tools.adapters.search import redact_secrets

    samples = {
        "httpx-style": "Client error '400' for url 'https://serpapi.com/search?q=hi&api_key=SECRETVAL&num=5'",
        "bare-key": "failed with key=SECRETVAL&hl=cn",
        "colon-form": "api_key: SECRETVAL and token=SECRETVAL",
        "bearer": "Authorization: Bearer SECRETVAL",
    }
    for label, text in samples.items():
        assert "SECRETVAL" not in redact_secrets(text), label


def test_redact_secrets_keeps_ordinary_words():
    """\b 是必要的：少了它 ``monkey=1`` 会连 ``key=1`` 一起被打掉。"""
    from agentix.tools.adapters.search import redact_secrets

    for text in ["SELECT monkey=1", "donkey=2 and hockey=3", "q=hello&num=5"]:
        assert redact_secrets(text) == text


def test_search_error_path_logs_masked_key_only():
    """真跑一遍出错分支：HTTP 400 时日志里必须只剩 ``api_key=***``。"""
    import http.server
    import socketserver
    import threading

    from agentix.tools.adapters import search as search_module

    class _Reject(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"nope")

        def log_message(self, *args):
            return

    captured: list[str] = []

    class _Capture:
        def error(self, message, *args):
            captured.append(message.format(*args))

        def info(self, message, *args):
            return

        def warning(self, message, *args):
            return

    server = socketserver.TCPServer(("127.0.0.1", 0), _Reject)
    port = server.server_address[1]
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        tool = search_module.SearchTool(
            api_key="LEAK_MARKER_G4", endpoint=f"http://127.0.0.1:{port}/search"
        )
        tool.allow_network = True
        tool.timeout = 5.0
        monkeypatched = _Capture()
        original = search_module.logger
        search_module.logger = monkeypatched
        try:
            assert tool.search("hello") == []
        finally:
            search_module.logger = original
    finally:
        server.shutdown()
        server.server_close()

    assert captured, "error branch did not log"
    assert all("LEAK_MARKER_G4" not in line for line in captured), captured
    assert any("api_key=***" in line for line in captured), captured


# ---------------------------------------------------------------------------
# G5：Text2SQL 单一词法（两闸一致 + 消误杀 + 补漏拦）
# ---------------------------------------------------------------------------

READ_ONLY_SQL = [
    "SELECT * FROM t WHERE note LIKE '%update %'",
    "SELECT REPLACE(name, 'a', 'b') FROM t",
    "SELECT gmt_update FROM orders",
    "SELECT * FROM t WHERE a = ';'",
    "WITH c AS (SELECT 1 AS a) SELECT a FROM c",
    "EXPLAIN SELECT * FROM t",
    "SHOW TABLES",
]

DANGEROUS_SQL = [
    "UPDATE users SET name = 'x'",
    "DELETE FROM t WHERE id = 1",
    "DROP TABLE t",
    "INSERT INTO t VALUES (1)",
    "SELECT 1; DROP TABLE t",
    "SELECT LOAD_FILE('/etc/passwd')",
    "SELECT * FROM t INTO OUTFILE '/tmp/x'",
    "SELECT SLEEP(10)",
    "SELECT BENCHMARK(100000000, MD5('a'))",
    "SELECT id FROM t FOR UPDATE",
]


@pytest.mark.parametrize("sql", READ_ONLY_SQL)
def test_lexicon_allows_read_only_statements(sql):
    ok, reasons = check_read_only(sql)
    assert ok, reasons


@pytest.mark.parametrize("sql", DANGEROUS_SQL)
def test_lexicon_blocks_dangerous_statements(sql):
    assert not check_read_only(sql)[0]


def test_strip_literals_removes_string_content():
    sql = "SELECT 'DROP TABLE x' AS a"
    stripped = strip_literals(sql)
    assert len(stripped) == len(sql)
    assert "DROP" not in stripped
    assert stripped.endswith("AS a")


def test_find_dangerous_reports_the_matching_pattern():
    assert "LOAD_FILE" in find_dangerous("SELECT LOAD_FILE('/x')")
    assert "INTO OUTFILE" in find_dangerous("SELECT 1 INTO OUTFILE '/x'")


def test_both_text2sql_gates_agree():
    """两闸各持一份黑名单时，同一句 SQL 会一闸过一闸拦——等于没有闸。"""
    from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2sql.sql_execution.node import (
        _is_read_only_query,
    )

    for sql in READ_ONLY_SQL + DANGEROUS_SQL:
        assert _is_read_only_query(sql) == check_read_only(sql)[0], sql


def test_security_validator_uses_the_same_lexicon():
    from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2sql.sql_validation.validators import (
        validate_sql_security,
    )

    for sql in DANGEROUS_SQL:
        ok, warnings = validate_sql_security(sql)
        assert not ok and warnings, sql
    assert validate_sql_security("SELECT * FROM t WHERE note LIKE '%update %'")[0]


def test_syntax_gate_keeps_the_narrow_select_with_rule():
    """语法面不得跟着执行面一起放宽——`SHOW TABLES` 必须是词法失败（test_p85 基线）。"""
    from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2sql.sql_validation.validators import (
        validate_sql_syntax,
    )

    assert validate_sql_syntax("SELECT id FROM t")[0]
    assert validate_sql_syntax("WITH c AS (SELECT 1 AS a) SELECT a FROM c")[0]
    assert not validate_sql_syntax("SHOW TABLES")[0]
    assert not validate_sql_syntax("EXPLAIN SELECT 1")[0]


def test_syntax_gate_no_longer_false_positives_on_escaped_quotes():
    """旧版在原文上数引号，``SELECT 'it''s'`` 因引号数为奇数被误判成语法错。"""
    from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2sql.sql_validation.validators import (
        validate_sql_syntax,
    )

    assert validate_sql_syntax("SELECT 'it''s' AS a")[0]
    assert validate_sql_syntax("SELECT * FROM t WHERE a = '('")[0]
    assert not validate_sql_syntax("SELECT 'unterminated")[0]
    assert not validate_sql_syntax("SELECT (1")[0]


# ---------------------------------------------------------------------------
# G1：同步 handler 下放线程后超时才生效
# ---------------------------------------------------------------------------


def _blocking_handler(args: dict) -> dict:
    time.sleep(1.1)
    return {"ok": True}


async def _async_handler(args: dict) -> dict:
    await asyncio.sleep(1.1)
    return {"ok": True}


async def _time_tool(handler, name: str) -> tuple[float, bool]:
    from agentix.tools.definitions import ToolSpec
    from agentix.tools.gateway.executor import ToolExecutor

    executor = ToolExecutor()
    executor.register_handler(
        name, handler, spec=ToolSpec(name=name, description="probe", risk_level="L0", timeout_ms=100)
    )
    started = time.perf_counter()
    result = await executor.execute(name, {})
    return (time.perf_counter() - started) * 1000, result.success


@pytest.mark.asyncio
async def test_sync_offload_flag_off_keeps_legacy_behaviour(monkeypatch):
    """flag 关 = 同步 handler 占住事件循环，timeout_ms 形同虚设（加固前基线）。"""
    monkeypatch.setattr(settings, "TOOL_SYNC_OFFLOAD_ENABLED", False)
    elapsed, success = await _time_tool(_blocking_handler, "p107_blk_off")
    assert success is True
    assert elapsed > 1000, elapsed


@pytest.mark.asyncio
async def test_sync_offload_flag_on_makes_timeout_effective(monkeypatch):
    monkeypatch.setattr(settings, "TOOL_SYNC_OFFLOAD_ENABLED", True)
    elapsed, success = await _time_tool(_blocking_handler, "p107_blk_on")
    assert success is False
    assert elapsed < 500, elapsed


@pytest.mark.asyncio
async def test_async_handler_truncated_regardless_of_flag(monkeypatch):
    for flag in (False, True):
        monkeypatch.setattr(settings, "TOOL_SYNC_OFFLOAD_ENABLED", flag)
        elapsed, success = await _time_tool(_async_handler, f"p107_async_{flag}")
        assert success is False
        assert elapsed < 500, (flag, elapsed)


@pytest.mark.asyncio
async def test_offloaded_sync_handler_still_returns_value(monkeypatch):
    from agentix.tools.definitions import ToolSpec
    from agentix.tools.gateway.executor import ToolExecutor

    monkeypatch.setattr(settings, "TOOL_SYNC_OFFLOAD_ENABLED", True)
    executor = ToolExecutor()
    executor.register_handler(
        "p107_quick",
        lambda args: {"echo": args.get("v")},
        spec=ToolSpec(name="p107_quick", description="d", risk_level="L0", timeout_ms=5000),
    )
    result = await executor.execute("p107_quick", {"v": 42})
    assert result.success is True
    assert result.data == {"echo": 42}


# ---------------------------------------------------------------------------
# G2：MCP 子进程环境白名单
# ---------------------------------------------------------------------------


def test_scrub_drops_secret_env_vars_and_keeps_os_critical():
    source = {
        "PATH": "/usr/bin",
        "SYSTEMROOT": "C:\\Windows",
        "LLM_API_KEY": "SECRET_G2",
        "SERPAPI_KEY": "SECRET_G2",
        "DATABASE_URL": "postgresql://u:pw@h/db",
        "OPENAI_API_KEY": "SECRET_G2",
    }
    scrubbed = _scrub_with(source)
    assert scrubbed["PATH"] == "/usr/bin"
    assert scrubbed["SYSTEMROOT"] == "C:\\Windows"
    for secret in ("LLM_API_KEY", "SERPAPI_KEY", "DATABASE_URL", "OPENAI_API_KEY"):
        assert secret not in scrubbed, secret


def test_scrub_honours_extra_key_allowlist(monkeypatch):
    monkeypatch.setattr(settings, "MCP_SUBPROCESS_ENV_EXTRA_KEYS", " MY_TOOL_FLAG,OTHER")
    scrubbed = _scrub_with({"PATH": "/usr/bin", "MY_TOOL_FLAG": "1", "OTHER": "2", "LLM_API_KEY": "x"})
    assert scrubbed["MY_TOOL_FLAG"] == "1"
    assert scrubbed["OTHER"] == "2"
    assert "LLM_API_KEY" not in scrubbed


def _scrub_with(source: dict) -> dict:
    from agentix.capabilities.mcp.client import scrub_subprocess_env

    return scrub_subprocess_env(source, project_root="/repo")


def test_scrub_restores_pythonpath_so_default_server_still_starts():
    """裁掉 PYTHONPATH 后 ``python -m agentix...`` 会找不到包，必须自动补回。"""
    scrubbed = _scrub_with({"PATH": "/usr/bin"})
    assert scrubbed["PYTHONPATH"] == "/repo"


@pytest.mark.asyncio
async def test_mcp_server_runs_with_scrub_on(monkeypatch):
    """真起子进程：scrub 打开后默认 server 仍能握手并算对 6*7。"""
    import sys

    from agentix.capabilities.mcp.client import MCPStdioClient

    monkeypatch.setattr(settings, "MCP_SUBPROCESS_ENV_SCRUB", True)
    async with MCPStdioClient([sys.executable, "-m", "agentix.capabilities.mcp.server"]) as client:
        names = {tool["name"] for tool in await client.list_tools()}
        assert {"calculator", "text_stats"} <= names
        payload = await client.call_tool("calculator", {"expression": "6*7"})
        assert '"value": 42' in payload["content"][0]["text"]


# ---------------------------------------------------------------------------
# G9：设置接口密钥掩码 + PUT 键白名单
# ---------------------------------------------------------------------------


def test_settings_response_masks_api_keys():
    from agentix.retrieval.api.routers import settings as settings_api

    masked = settings_api.mask_config({"llm_api_key": "ABCDEFGH1234", "llm_model": "qwen"})
    assert masked["llm_api_key"] == "***1234"
    assert masked["llm_model"] == "qwen"


def test_settings_masking_is_idempotent_and_skips_empty():
    from agentix.retrieval.api.routers.settings import mask_secret

    assert mask_secret("***1234") == "***1234"
    assert mask_secret("") == ""
    assert mask_secret(None) is None


def test_put_rejects_unknown_keys_and_drops_masked_secrets():
    """前端会把 GET 回来的掩码串原样 PUT 回来；照单全收等于把真 key 覆盖成 ***。"""
    from fastapi import HTTPException

    from agentix.retrieval.api.routers.settings import (
        ALLOWED_CONFIG_KEYS,
        is_masked_value,
        is_secret_field,
    )

    assert "llm_api_key" in ALLOWED_CONFIG_KEYS
    assert set(ALLOWED_CONFIG_KEYS) == {
        "llm_provider", "llm_base_url", "llm_api_key", "llm_model",
        "embedding_provider", "embedding_base_url", "embedding_api_key", "embedding_model",
        "reranker_model", "chunk_strategy", "chunk_mode", "chunk_size", "chunk_overlap",
        "search_k", "sparse_k", "dense_k", "fusion_k", "reranker_type",
    }
    assert is_secret_field("embedding_api_key") is True
    assert is_secret_field("chunk_size") is False
    assert is_masked_value("***1234") is True
    assert is_masked_value("realvalue") is False
    assert HTTPException is not None


def test_merged_config_never_returns_the_plaintext_marker(monkeypatch):
    """走真实 ``get_merged_config``：DEFAULT_CONFIG 里放入哨兵后响应体不得含原值。"""
    from agentix.retrieval.api.routers import settings as settings_api

    poisoned = dict(settings_api.DEFAULT_CONFIG)
    poisoned["llm_api_key"] = "P107_MARKER_LLM"
    poisoned["embedding_api_key"] = "P107_MARKER_EMB"
    monkeypatch.setattr(settings_api, "DEFAULT_CONFIG", poisoned, raising=False)

    merged = settings_api.get_merged_config(None)
    assert "P107_MARKER_LLM" not in str(merged)
    assert "P107_MARKER_EMB" not in str(merged)
    assert merged["llm_api_key"].startswith("***")


# ---------------------------------------------------------------------------
# G6：生产身份显式声明，不再由 DEBUG 反推
# ---------------------------------------------------------------------------


def test_app_env_takes_precedence_over_debug(monkeypatch):
    from agentix.retrieval.settings import settings as retrieval_settings

    monkeypatch.setattr(settings, "APP_ENV", "production")
    assert retrieval_settings.ENVIRONMENT == "production"
    assert retrieval_settings.PRODUCTION is True


def test_empty_app_env_falls_back_to_legacy_inference(monkeypatch):
    from agentix.retrieval.settings import settings as retrieval_settings

    monkeypatch.setattr(settings, "APP_ENV", "")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setattr(settings, "DEBUG", True)
    assert retrieval_settings.ENVIRONMENT == "development"
    monkeypatch.setattr(settings, "DEBUG", False)
    assert retrieval_settings.ENVIRONMENT == "production"


def test_legacy_environment_var_still_honoured(monkeypatch):
    from agentix.retrieval.settings import settings as retrieval_settings

    monkeypatch.setattr(settings, "APP_ENV", "")
    monkeypatch.setenv("ENVIRONMENT", "staging")
    assert retrieval_settings.ENVIRONMENT == "staging"


def test_production_gate_fires_even_when_debug_is_true(monkeypatch):
    """G6 的后果链核心：DEBUG 忘关 ⇒ 生产校验整段静默。显式 APP_ENV 必须堵住它。"""
    from agentix.retrieval.settings import settings as retrieval_settings
    from agentix.retrieval.settings import validate_app_settings

    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "DEBUG", True)
    errors, _ = validate_app_settings()
    assert any("RATE_LIMIT_ENABLED" in e for e in errors), errors


def test_staging_is_in_the_hard_fail_branch(monkeypatch):
    """G6 的连带面：`docker/compose.override.staging.yml` 显式写了 `APP_ENV: staging`。

    只判 PRODUCTION 会让 staging 从 raise 分支里**静默掉出去**（G6 之前它靠
    DEBUG=false 反推成 "production" 才拿到这道校验）。staging 必须留在硬失败面内。
    """
    from agentix.retrieval.settings import settings as retrieval_settings
    from agentix.retrieval.settings import validate_app_settings

    monkeypatch.setattr(settings, "APP_ENV", "staging")
    monkeypatch.setattr(settings, "DEBUG", False)
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    assert retrieval_settings.PRODUCTION is False, "staging 不该被当成生产身份"
    assert retrieval_settings.STRICT_VALIDATION is True
    errors, _ = validate_app_settings()
    assert any("RATE_LIMIT_ENABLED" in e for e in errors), errors


def test_staging_refuses_to_start_on_validation_errors(monkeypatch):
    """把上一条的口径跑到真正的启动路径上（不是只看布尔值）。"""
    import agentix.main as main_module

    monkeypatch.setattr(settings, "APP_ENV", "staging")
    monkeypatch.setattr(settings, "DEBUG", False)
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(main_module.logger, "warning", lambda *a, **k: None)
    monkeypatch.setattr(main_module.logger, "info", lambda *a, **k: None)
    monkeypatch.setattr(main_module.logger, "error", lambda *a, **k: None)
    with pytest.raises(RuntimeError) as excinfo:
        main_module._run_settings_validation()
    message = str(excinfo.value)
    assert "staging" in message, message
    assert "RATE_LIMIT_ENABLED" in message, message


def test_development_does_not_refuse_to_start(monkeypatch):
    """硬失败面不得扩大到开发环境——否则校验本身成了新的故障源。"""
    import agentix.main as main_module

    monkeypatch.setattr(settings, "APP_ENV", "development")
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(main_module.logger, "warning", lambda *a, **k: None)
    monkeypatch.setattr(main_module.logger, "info", lambda *a, **k: None)
    monkeypatch.setattr(main_module.logger, "error", lambda *a, **k: None)
    main_module._run_settings_validation()


def _capture_warnings(monkeypatch, main_module) -> list[str]:
    """loguru 的 ``warning(template, *args)`` 要先渲染再断言，否则只看得到模板。"""

    logged: list[str] = []

    def _record(message, *args, **kwargs):
        logged.append(str(message).format(*args) if args else str(message))

    monkeypatch.setattr(main_module.logger, "warning", _record)
    return logged


def test_posture_warning_lists_disabled_gates(monkeypatch):
    """G6 的可发现性：未声明 APP_ENV 时必须点名当前关掉的闸门。"""
    import agentix.main as main_module

    monkeypatch.setattr(settings, "APP_ENV", "")
    for flag in main_module._INSECURE_GATE_FLAGS:
        monkeypatch.setattr(settings, flag, False)
    logged = _capture_warnings(monkeypatch, main_module)
    main_module._warn_insecure_posture()
    assert len(logged) == 1
    for flag in main_module._INSECURE_GATE_FLAGS:
        assert flag in logged[0], flag


def test_posture_warning_silent_when_gates_all_on(monkeypatch):
    """闸门全开就不再唠叨——噪音会让真告警被忽略。"""
    import agentix.main as main_module

    monkeypatch.setattr(settings, "APP_ENV", "")
    for flag in main_module._INSECURE_GATE_FLAGS:
        monkeypatch.setattr(settings, flag, True)
    logged = _capture_warnings(monkeypatch, main_module)
    main_module._warn_insecure_posture()
    assert logged == []


def test_posture_warning_silent_when_app_env_declared(monkeypatch):
    """显式声明即代表运维做过判断，再叫就是噪音。"""
    import agentix.main as main_module

    monkeypatch.setattr(settings, "APP_ENV", "development")
    logged = _capture_warnings(monkeypatch, main_module)
    main_module._warn_insecure_posture()
    assert logged == []


# ---------------------------------------------------------------------------
# G11：DB_ECHO 覆盖 DEBUG 推断
# ---------------------------------------------------------------------------


def test_db_echo_field_defaults_to_none(monkeypatch):
    """None = 沿用旧行为（由 DEBUG 推断），保证不配新键时逐位等价。"""
    assert type(settings).model_fields["DB_ECHO"].default is None
    assert type(settings).model_fields["UVICORN_RELOAD"].default is None


def test_database_module_honours_explicit_db_echo_false():
    """``echo=True`` 会把完整 SQL 连参数打进标准库 logging，绕过 loguru 出口。"""
    import subprocess

    code = (
        "from agentix.infrastructure.core.database import engine;"
        "print('ECHO=' + str(engine.echo))"
    )
    off = _run_with_env(code, {"DB_ECHO": "false", "DEBUG": "true"})
    on = _run_with_env(code, {"DB_ECHO": "true", "DEBUG": "false"})
    legacy_debug = _run_with_env(code, {"DEBUG": "true"})
    assert "ECHO=False" in off, off
    assert "ECHO=True" in on, on
    assert "ECHO=True" in legacy_debug, legacy_debug


def _run_with_env(code: str, env: dict[str, str]) -> str:
    import os
    import subprocess
    import sys

    child = {**os.environ, **env, "PYTHONPATH": os.getcwd()}
    for key in ("DB_ECHO", "DEBUG"):
        if key not in env:
            child.pop(key, None)
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=child, timeout=180
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


# ---------------------------------------------------------------------------
# 新 flag 的出厂默认必须全是「关」
# ---------------------------------------------------------------------------


def test_sandbox_flags_default_off():
    """§0.5 硬约定：新能力 = 新 flag + 默认关 + flag-off 与基线逐位等价。"""
    fields = type(settings).model_fields
    assert fields["TOOL_SYNC_OFFLOAD_ENABLED"].default is False
    assert fields["MCP_SUBPROCESS_ENV_SCRUB"].default is False
    assert fields["MCP_SUBPROCESS_ENV_EXTRA_KEYS"].default == ""
    assert fields["APP_ENV"].default == ""
