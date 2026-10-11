"""Wren 语义层 Text2SQL 适配器测试（零 wrenai 依赖：fake toolkit 注入）。

覆盖：
- 工厂 get_wren_backend：默认 mysql → None；wren + 路径空 → None；
  wren + 项目无效 → None（降级）
- WrenSQLBackend：SDK 通道（fake WrenToolkit）dry_plan/validate/execute
  行箭头映射与 max_rows 截断
- 双通道降级：wren_langchain 缺失时落 WrenEngine 直连通道；wrenai 缺失 →
  WrenNotAvailable
- sql_execution 节点接线：wren 成功 / 降级回 MySQL 的 steps 留痕

注意：本文件必须**先导入** ``agentix.application.agents.text2sql``——
kg_sub_graph 与 application 两个 text2sql 包存在既有循环导入（生产代码
即以 application 先入的顺序工作），直接先导 kg 包会触发部分初始化错误。
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

import agentix.application.agents.text2sql  # noqa: F401,E402 - 先入打破既有循环导入

from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2sql.sql_execution import (  # noqa: E501,E402
    node as exec_node,
)
from agentix.config import settings  # noqa: E402
from agentix.infrastructure.text2sql.wren_adapter import (  # noqa: E402
    WrenNotAvailable,
    WrenSQLBackend,
    get_wren_backend,
)


class _FakeTable:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def to_pylist(self) -> list[dict]:
        return self._rows


class _FakeToolkit:
    """模拟 wren_langchain.WrenToolkit 的最小接口。"""

    def __init__(self, rows: list[dict] | None = None, *, fail: bool = False) -> None:
        self.rows = rows if rows is not None else [{"law_name": "民法典", "count": 1}]
        self.fail = fail
        self.planned_sql: str | None = None
        self.executed_sql: str | None = None

    def dry_plan(self, sql: str) -> str:
        self.planned_sql = f"-- mdl-expanded\n{sql}"
        return self.planned_sql

    def dry_run(self, sql: str) -> None:
        if self.fail:
            raise ValueError("MDL validation failed")

    def query(self, sql: str, limit: int | None = None, properties=None):  # type: ignore[no-untyped-def]
        self.executed_sql = sql
        return _FakeTable(self.rows[:limit] if limit else self.rows)


@pytest.fixture()
def wren_project(tmp_path: Path) -> Path:
    """伪造一个 CLI 准备好的 Wren 项目（wren_project.yml + target/mdl.json）。"""
    (tmp_path / "wren_project.yml").write_text("version: 1\n", encoding="utf-8")
    target = tmp_path / "target"
    target.mkdir()
    (target / "mdl.json").write_text(json.dumps({"models": []}), encoding="utf-8")
    return tmp_path


@pytest.fixture()
def _restore_settings():
    saved = (settings.TEXT2SQL_BACKEND, settings.WREN_PROJECT_PATH)
    yield
    settings.TEXT2SQL_BACKEND, settings.WREN_PROJECT_PATH = saved


# -- 工厂 -------------------------------------------------------------------

def test_factory_disabled_by_default(_restore_settings):
    settings.TEXT2SQL_BACKEND = "mysql"
    assert get_wren_backend() is None


def test_factory_wren_without_path_falls_back(_restore_settings):
    settings.TEXT2SQL_BACKEND = "wren"
    settings.WREN_PROJECT_PATH = ""
    assert get_wren_backend() is None


def test_factory_wren_invalid_project_falls_back(_restore_settings, tmp_path):
    settings.TEXT2SQL_BACKEND = "wren"
    settings.WREN_PROJECT_PATH = str(tmp_path)  # 无 target/mdl.json
    assert get_wren_backend() is None


def test_factory_wren_valid_project(_restore_settings, wren_project):
    settings.TEXT2SQL_BACKEND = "wren"
    settings.WREN_PROJECT_PATH = str(wren_project)
    backend = get_wren_backend()
    assert isinstance(backend, WrenSQLBackend)


# -- 适配器（SDK 通道，fake 注入）--------------------------------------------

def test_backend_dry_plan_via_sdk_channel(wren_project):
    backend = WrenSQLBackend(wren_project)
    fake = _FakeToolkit()
    backend._toolkit = fake  # 直接注入，绕过真实 import（wrenai 未安装）
    out = backend.dry_plan("SELECT count(*) FROM laws")
    assert out.startswith("-- mdl-expanded")
    assert fake.planned_sql == out


def test_backend_execute_maps_rows_and_truncates(wren_project):
    backend = WrenSQLBackend(wren_project)
    fake = _FakeToolkit(rows=[{"i": i} for i in range(10)])
    backend._toolkit = fake
    rows = backend.execute("SELECT i FROM t", max_rows=3)
    assert len(rows) == 3 and rows[0] == {"i": 0}
    assert fake.executed_sql == "SELECT i FROM t"


def test_backend_validate_failure_raises_value_error(wren_project):
    backend = WrenSQLBackend(wren_project)
    backend._toolkit = _FakeToolkit(fail=True)
    with pytest.raises(ValueError):
        backend.validate("SELECT 1")


# -- 双通道降级 ---------------------------------------------------------------

def _forbid_wren_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    """让所有 wren* 模块导入失败（模拟 wrenai 未安装）。"""
    import builtins

    real_import = builtins.__import__

    def _no_wren(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "wren" or name.startswith("wren.") or name.startswith("wren_"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_wren)


def test_backend_without_wrenai_raises_not_available(wren_project, monkeypatch):
    """两个通道都 import 失败 → WrenNotAvailable（降级语义的源头）。"""
    _forbid_wren_imports(monkeypatch)
    backend = WrenSQLBackend(wren_project)
    with pytest.raises(WrenNotAvailable):
        backend.execute("SELECT 1")


def test_backend_falls_back_to_direct_engine_channel(wren_project, monkeypatch):
    """wren_langchain 缺失但 wrenai（wren.engine）可用 → 直连通道工作。"""
    monkeypatch.setattr(settings, "WREN_DATA_SOURCE", "duckdb", raising=False)

    class _FakeEngine:
        def __init__(self, *, manifest_str: str, data_source: str, connection_info: dict):
            assert manifest_str  # manifest 已 base64 编码注入
            assert data_source == "duckdb"

        def dry_plan(self, sql: str) -> str:
            return sql

        def dry_run(self, sql: str) -> None:
            return None

        def query(self, sql: str, limit=None, properties=None):  # type: ignore[no-untyped-def]
            return _FakeTable([{"ok": True}])

    fake_pkg = types.ModuleType("wren")
    fake_mod = types.ModuleType("wren.engine")
    fake_mod.WrenEngine = _FakeEngine
    fake_pkg.engine = fake_mod
    monkeypatch.setitem(sys.modules, "wren", fake_pkg)
    monkeypatch.setitem(sys.modules, "wren.engine", fake_mod)

    backend = WrenSQLBackend(wren_project)
    rows = backend.execute("SELECT 1")
    assert rows == [{"ok": True}]


# -- 节点接线 -----------------------------------------------------------------

def _base_state() -> dict:
    return {"sql_statement": "SELECT 1 AS one", "is_valid": True, "max_rows": 10}


@pytest.mark.asyncio
async def test_node_uses_wren_backend_when_available(_restore_settings, wren_project):
    real_backend = WrenSQLBackend(wren_project)
    real_backend._toolkit = _FakeToolkit(rows=[{"one": 1}])
    with patch.object(exec_node, "get_wren_backend", lambda: real_backend):
        state = await exec_node.create_sql_execution_node()(_base_state())
    assert state["execution_error"] is None
    assert state["steps"] == ["sql_execution_wren"]
    assert state["execution_results"] == [{"one": 1}]


@pytest.mark.asyncio
async def test_node_keeps_mysql_semantics_when_wren_disabled(
    _restore_settings, monkeypatch
):
    """默认（未启用 wren）→ 走 MySQL 路径，steps 留痕与既有行为完全一致。"""
    monkeypatch.setattr(exec_node, "get_wren_backend", lambda: None)

    def _fake_run(connection_string: str, sql: str, max_rows: int):
        return [{"one": 1}]

    with patch.object(exec_node, "_run_query_sync", _fake_run):
        state = await exec_node.create_sql_execution_node()(_base_state())
    assert state["execution_error"] is None
    assert state["steps"] == ["sql_execution"]


@pytest.mark.asyncio
async def test_node_marks_wren_degraded_step(_restore_settings, monkeypatch):
    """wren 通道抛 WrenNotAvailable → 降级 MySQL 且 steps 留痕。"""

    class _BrokenBackend:
        def validate(self, sql: str) -> None:
            raise WrenNotAvailable("wrenai not installed")

    monkeypatch.setattr(exec_node, "get_wren_backend", lambda: _BrokenBackend())

    def _fake_run(connection_string: str, sql: str, max_rows: int):
        return [{"one": 1}]

    with patch.object(exec_node, "_run_query_sync", _fake_run):
        state = await exec_node.create_sql_execution_node()(_base_state())
    assert state["execution_error"] is None
    assert state["steps"] == ["sql_execution_wren_degraded"]
