# -*- coding: utf-8 -*-
"""F2b-c · Stage F2：R-P2-4 Text2SQL 加固守卫（rag-optimization §6）。

覆盖：
1. 键同步（settings 默认 / selfcheck / .env.example / configuration.md 双落表 / p41 清单）
2. 显式失败（correctness，无 flag）：formatter 不再把未执行 SQL 包装成结果摘要
3. 路由器重试耗尽 → end 契约
4. dry-run（`TEXT2SQL_DRY_RUN_ENABLED` 默认关）：EXPLAIN 试跑喂重试环、
   词法失败不试跑、基础设施故障 fail-open
5. schema 缓存（`TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS` 默认 0=关）：命中/换题/失败不入缓存
6. Wren 缺失显式降级打点（steps + `record_degraded("text2sql_wren")`）
"""

from __future__ import annotations

import pytest

import agentix.application.agents.text2sql  # noqa: F401,E402 - 先入打破既有循环导入

from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2sql.formatting.node import (  # noqa: E501,E402
    SQL_NOT_EXECUTED_ANSWER,
    SQL_NOT_EXECUTED_ERROR,
    create_answer_formatter_node,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2sql.schema_retrieval import (  # noqa: E501,E402
    node as schema_node_mod,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2sql.sql_execution import (  # noqa: E501,E402
    node as exec_node,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2sql.sql_validation import (  # noqa: E501,E402
    node as validation_node_mod,
)
from agentix.config.settings import settings as main_settings  # noqa: E402


# ===========================================================================
# 1. 键同步
# ===========================================================================


class TestKeySync:
    def test_settings_defaults(self):
        assert main_settings.TEXT2SQL_DRY_RUN_ENABLED is False
        assert main_settings.TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS == 0

    def test_selfcheck_ignores_missing(self):
        from agentix.config.selfcheck import DEFAULT_IGNORED_MISSING

        assert "TEXT2SQL_DRY_RUN_ENABLED" in DEFAULT_IGNORED_MISSING
        assert "TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS" in DEFAULT_IGNORED_MISSING

    def test_env_example_lines(self):
        from pathlib import Path

        text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(
            encoding="utf-8"
        )
        assert "TEXT2SQL_DRY_RUN_ENABLED=false" in text
        assert "TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS=0" in text

    def test_configuration_md_double_entry(self):
        from pathlib import Path

        text = (
            Path(__file__).resolve().parents[1]
            / "docs/architecture/configuration.md"
        ).read_text(encoding="utf-8")
        for key in ("TEXT2SQL_DRY_RUN_ENABLED", "TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS"):
            assert text.count(f"`{key}`") >= 2, f"{key} 未在 configuration.md 双落表"

    def test_p41_manual_list_contains_new_keys(self):
        from pathlib import Path

        text = (
            Path(__file__).resolve().parents[1]
            / "tests/test_p41_doc_consistency.py"
        ).read_text(encoding="utf-8")
        assert '"TEXT2SQL_DRY_RUN_ENABLED"' in text
        assert '"TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS"' in text


# ===========================================================================
# 2. 显式失败（formatter）
# ===========================================================================


def _fmt(state: dict) -> dict:
    import asyncio

    return asyncio.run(create_answer_formatter_node()(state))


class TestFormatterExplicitFailure:
    def test_retries_exhausted_explicit_failure(self):
        out = _fmt(
            {
                "is_valid": False,
                "sql_statement": "SELCT bad sql",
                "retry_count": 3,
                "max_retries": 3,
                "question": "有多少合同？",
                "execution_results": None,
            }
        )
        assert out["answer"] == SQL_NOT_EXECUTED_ANSWER
        assert out["execution_error"] == SQL_NOT_EXECUTED_ERROR
        assert "查询结果摘要" not in out["answer"]
        assert out["steps"] == ["format_answer"]

    def test_empty_sql_path_also_explicit(self):
        out = _fmt({"is_valid": False, "sql_statement": "", "question": "q"})
        assert out["answer"] == SQL_NOT_EXECUTED_ANSWER
        assert out["execution_error"] == SQL_NOT_EXECUTED_ERROR

    def test_execute_error_path_preserved(self):
        out = _fmt(
            {
                "is_valid": True,
                "execution_error": "connection refused",
                "sql_statement": "SELECT 1",
                "question": "q",
            }
        )
        assert out["answer"] == "抱歉，执行 SQL 时出现错误：connection refused"
        assert out["execution_error"] == "connection refused"

    def test_valid_path_unchanged(self):
        out = _fmt(
            {
                "is_valid": True,
                "sql_statement": "SELECT 1 AS one",
                "question": "有多少合同？",
                "analysis_text": "共 3 份。",
                "execution_results": [{"one": 1}],
            }
        )
        assert out["execution_error"] is None
        assert "查询结果摘要" in out["answer"]
        assert "结果预览" in out["answer"]
        assert "共 3 份。" in out["answer"]

    def test_tool_consumes_execution_error(self):
        from pathlib import Path

        tool_src = (
            Path(__file__).resolve().parents[1]
            / "agentix/application/agents/kg_sub_graph/agentic_rag_agents/"
            "components/text2cypher/text2sql_tool.py"
        ).read_text(encoding="utf-8")
        assert "if execution_error:" in tool_src
        assert "errors.append" in tool_src


# ===========================================================================
# 3. 路由器重试耗尽契约
# ===========================================================================


class TestRouterExhaustion:
    def test_exhausted_routes_end(self):
        from agentix.application.agents.text2sql.workflow import (
            _should_execute_or_retry,
        )

        assert (
            _should_execute_or_retry(
                {"is_valid": False, "retry_count": 3, "max_retries": 3}
            )
            == "end"
        )

    def test_retry_below_limit(self):
        from agentix.application.agents.text2sql.workflow import (
            _should_execute_or_retry,
        )

        assert (
            _should_execute_or_retry(
                {"is_valid": False, "retry_count": 1, "max_retries": 3}
            )
            == "retry"
        )

    def test_valid_executes(self):
        from agentix.application.agents.text2sql.workflow import (
            _should_execute_or_retry,
        )

        assert (
            _should_execute_or_retry(
                {"is_valid": True, "retry_count": 0, "max_retries": 3}
            )
            == "execute"
        )


# ===========================================================================
# 4. dry-run
# ===========================================================================


def _spy_dry_run(monkeypatch, ok=True, err="dry-run（EXPLAIN）未通过：boom"):
    from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2sql.sql_validation import (
        dry_run as dry_mod,
    )

    calls: list[str] = []

    def fake(sql: str):
        calls.append(sql)
        return (True, "") if ok else (False, err)

    monkeypatch.setattr(dry_mod, "dry_run_explain", fake)
    return calls


@pytest.fixture()
def dry_flag(monkeypatch):
    monkeypatch.setattr(main_settings, "TEXT2SQL_DRY_RUN_ENABLED", True)
    return monkeypatch


class TestDryRunValidation:
    @pytest.mark.asyncio
    async def test_flag_off_never_calls_dry_run(self, monkeypatch):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2sql.sql_validation import (
            dry_run as dry_mod,
        )

        monkeypatch.setattr(main_settings, "TEXT2SQL_DRY_RUN_ENABLED", False)

        def _boom(sql):  # pragma: no cover
            raise AssertionError("dry-run must not run when flag off")

        monkeypatch.setattr(dry_mod, "dry_run_explain", _boom)
        node = validation_node_mod.create_sql_validation_node()
        out = await node({"sql_statement": "SELECT 1 AS one", "retry_count": 0})
        assert out["is_valid"] is True
        assert out["steps"] == ["sql_validation"]

    @pytest.mark.asyncio
    async def test_dry_run_failure_feeds_retry(self, dry_flag):
        calls = _spy_dry_run(monkeypatch=dry_flag, ok=False)
        node = validation_node_mod.create_sql_validation_node()
        out = await node({"sql_statement": "SELECT 1 AS one", "retry_count": 1})
        assert out["is_valid"] is False
        assert out["retry_count"] == 2
        assert out["steps"] == ["sql_validation_failed"]
        assert any("dry-run" in e for e in out["validation_errors"])
        assert calls == ["SELECT 1 AS one"]

    @pytest.mark.asyncio
    async def test_dry_run_pass_keeps_valid(self, dry_flag):
        _spy_dry_run(monkeypatch=dry_flag, ok=True)
        node = validation_node_mod.create_sql_validation_node()
        out = await node({"sql_statement": "SELECT 1 AS one", "retry_count": 0})
        assert out["is_valid"] is True
        assert out["retry_count"] == 0

    @pytest.mark.asyncio
    async def test_lexical_failure_skips_dry_run(self, dry_flag):
        calls = _spy_dry_run(monkeypatch=dry_flag, ok=False)
        node = validation_node_mod.create_sql_validation_node()
        out = await node({"sql_statement": "SHOW TABLES", "retry_count": 0})
        assert out["is_valid"] is False, "非 SELECT 词法失败"
        assert calls == [], "词法失败不应再试跑"

    @pytest.mark.asyncio
    async def test_empty_sql_short_circuits(self, dry_flag):
        calls = _spy_dry_run(monkeypatch=dry_flag, ok=False)
        node = validation_node_mod.create_sql_validation_node()
        out = await node({"sql_statement": "", "retry_count": 0})
        assert out["is_valid"] is False
        assert calls == []


class TestDryRunExplain:
    def test_infra_error_classifier(self):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2sql.sql_validation.dry_run import (
            _is_infra_error,
        )

        assert _is_infra_error(Exception("Can't connect to MySQL server"))
        assert _is_infra_error(Exception("Access denied for user 'x'"))
        assert _is_infra_error(Exception("Communications link failure"))
        assert not _is_infra_error(
            Exception("You have an error in your SQL syntax near 'SELCT'")
        )
        assert not _is_infra_error(Exception("Table 'db.t' doesn't exist"))

    @pytest.mark.asyncio
    async def test_no_connection_string_fail_open(self, monkeypatch):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2sql.sql_validation.dry_run import (
            dry_run_explain,
        )

        monkeypatch.setattr(main_settings, "DATABASE_URL", "")
        assert dry_run_explain("SELECT 1") == (True, "")

    @pytest.mark.asyncio
    async def test_sqlite_explain_ok_and_reject(self, monkeypatch):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2sql.sql_validation.dry_run import (
            dry_run_explain,
        )

        monkeypatch.setattr(main_settings, "DATABASE_URL", "sqlite://")
        ok, err = dry_run_explain("SELECT 1 AS one")
        assert ok and err == ""
        bad_ok, bad_err = dry_run_explain("THIS IS NOT SQL")
        assert bad_ok is False
        assert "dry-run" in bad_err


# ===========================================================================
# 5. schema 缓存
# ===========================================================================


@pytest.fixture()
def schema_cache(monkeypatch):
    schema_node_mod._SCHEMA_CACHE.clear()
    monkeypatch.setattr(main_settings, "TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS", 0)
    yield monkeypatch
    schema_node_mod._SCHEMA_CACHE.clear()


class TestSchemaCache:
    @pytest.mark.asyncio
    async def test_ttl_zero_fetches_every_time(self, schema_cache):
        calls: list[str] = []

        async def fake_fetch(question: str):
            calls.append(question)
            return {"tables": [{"table_name": "t1"}], "relationships": []}

        schema_cache.setattr(schema_node_mod, "_retrieve_schema_from_mysql", fake_fetch)
        node = schema_node_mod.create_schema_retrieval_node()
        await node({"question": "有多少合同？"})
        await node({"question": "有多少合同？"})
        assert len(calls) == 2, "TTL=0 = 关闭缓存，每问现拉"

    @pytest.mark.asyncio
    async def test_ttl_on_caches_same_question(self, schema_cache):
        schema_cache.setattr(main_settings, "TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS", 60)
        calls: list[str] = []

        async def fake_fetch(question: str):
            calls.append(question)
            return {"tables": [{"table_name": "t1"}], "relationships": []}

        schema_cache.setattr(schema_node_mod, "_retrieve_schema_from_mysql", fake_fetch)
        node = schema_node_mod.create_schema_retrieval_node()
        first = await node({"question": "有多少合同？"})
        second = await node({"question": "有多少合同？"})
        assert len(calls) == 1
        assert first["schema_context"] == second["schema_context"]
        assert first["steps"] == ["schema_retrieval"]
        assert second["steps"] == ["schema_retrieval"]
        # 归一入键：大小写/空白差异同池
        await node({"question": "  有多少合同？  "})
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_different_question_refetches(self, schema_cache):
        schema_cache.setattr(main_settings, "TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS", 60)
        calls: list[str] = []

        async def fake_fetch(question: str):
            calls.append(question)
            return {"tables": [], "relationships": []}

        schema_cache.setattr(schema_node_mod, "_retrieve_schema_from_mysql", fake_fetch)
        node = schema_node_mod.create_schema_retrieval_node()
        await node({"question": "问题A"})
        await node({"question": "问题B"})
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_failure_not_cached(self, schema_cache):
        schema_cache.setattr(main_settings, "TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS", 60)
        calls: list[str] = []

        async def fake_fetch(question: str):
            calls.append(question)
            if len(calls) == 1:
                raise RuntimeError("db down")
            return {"tables": [], "relationships": []}

        schema_cache.setattr(schema_node_mod, "_retrieve_schema_from_mysql", fake_fetch)
        node = schema_node_mod.create_schema_retrieval_node()
        first = await node({"question": "问题"})
        assert first["steps"] == ["schema_retrieval_failed"]
        second = await node({"question": "问题"})
        assert second["steps"] == ["schema_retrieval"]
        assert len(calls) == 2, "失败结果不得入缓存"

    @pytest.mark.asyncio
    async def test_cache_hit_metric(self, schema_cache):
        from prometheus_client import REGISTRY

        schema_cache.setattr(main_settings, "TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS", 60)

        async def fake_fetch(question: str):
            return {"tables": [], "relationships": []}

        schema_cache.setattr(schema_node_mod, "_retrieve_schema_from_mysql", fake_fetch)
        node = schema_node_mod.create_schema_retrieval_node()

        def _hits():
            return (
                REGISTRY.get_sample_value(
                    "cache_hits_total", {"cache_type": "text2sql_schema"}
                )
                or 0.0
            )

        before = _hits()
        await node({"question": "指标题"})
        await node({"question": "指标题"})
        assert _hits() == before + 1


# ===========================================================================
# 6. Wren 缺失显式降级打点
# ===========================================================================


def _exec_base() -> dict:
    return {"sql_statement": "SELECT 1 AS one", "is_valid": True, "max_rows": 10}


class TestWrenDegradationMetric:
    @pytest.mark.asyncio
    async def test_wren_requested_but_missing_degrades_and_counts(
        self, monkeypatch
    ):
        monkeypatch.setattr(main_settings, "TEXT2SQL_BACKEND", "wren")
        monkeypatch.setattr(exec_node, "get_wren_backend", lambda: None)
        monkeypatch.setattr(
            exec_node, "_run_query_sync", lambda conn, sql, max_rows: [{"one": 1}]
        )
        degraded: list[str] = []

        import agentix.observability.metrics as metrics_mod

        real = metrics_mod.record_degraded
        monkeypatch.setattr(
            metrics_mod,
            "record_degraded",
            lambda component: (degraded.append(component), real(component)),
        )
        node = exec_node.create_sql_execution_node(connection_string="sqlite://")
        out = await node(_exec_base())
        assert out["steps"] == ["sql_execution_wren_degraded"]
        assert out["execution_results"] == [{"one": 1}]
        assert "text2sql_wren" in degraded

    @pytest.mark.asyncio
    async def test_mysql_default_no_degraded_metric(self, monkeypatch):
        monkeypatch.setattr(main_settings, "TEXT2SQL_BACKEND", "mysql")
        monkeypatch.setattr(exec_node, "get_wren_backend", lambda: None)
        monkeypatch.setattr(
            exec_node, "_run_query_sync", lambda conn, sql, max_rows: [{"one": 1}]
        )
        degraded: list[str] = []

        import agentix.observability.metrics as metrics_mod

        monkeypatch.setattr(
            metrics_mod, "record_degraded", lambda component: degraded.append(component)
        )
        node = exec_node.create_sql_execution_node(connection_string="sqlite://")
        out = await node(_exec_base())
        assert out["steps"] == ["sql_execution"]
        assert degraded == [], "默认后端无 Wren 语义，不得误报降级"

    @pytest.mark.asyncio
    async def test_wren_exception_degradation_also_counts(self, monkeypatch):
        from agentix.infrastructure.text2sql.wren_adapter import WrenNotAvailable

        class _Broken:
            def validate(self, sql: str) -> None:
                raise WrenNotAvailable("wrenai not installed")

        monkeypatch.setattr(main_settings, "TEXT2SQL_BACKEND", "wren")
        monkeypatch.setattr(exec_node, "get_wren_backend", lambda: _Broken())
        monkeypatch.setattr(
            exec_node, "_run_query_sync", lambda conn, sql, max_rows: [{"one": 1}]
        )
        degraded: list[str] = []

        import agentix.observability.metrics as metrics_mod

        monkeypatch.setattr(
            metrics_mod, "record_degraded", lambda component: degraded.append(component)
        )
        node = exec_node.create_sql_execution_node(connection_string="sqlite://")
        out = await node(_exec_base())
        assert out["steps"] == ["sql_execution_wren_degraded"]
        assert degraded == ["text2sql_wren"]
