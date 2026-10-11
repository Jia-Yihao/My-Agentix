"""P0-S1 止血修复回归验证（QA，不修改源码）。

覆盖：
1. agentix/config/settings.py — SECRET_KEY 固化 + resolved_secret_key 三分支
2. multi_tool.py — error_tool_selection 节点已注册且有到 summarize 的出边
"""
import inspect
import logging

import pytest
from unittest.mock import MagicMock

from agentix.config.settings import Settings


def _clean_env(monkeypatch):
    """隔离环境变量与 .env，避免外部配置污染分支判定。"""
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("DEBUG", raising=False)


# ---------------------------------------------------------------
# 1) resolved_secret_key 三分支
# ---------------------------------------------------------------

def test_resolved_secret_key_explicit_config_wins(monkeypatch):
    _clean_env(monkeypatch)
    s = Settings(SECRET_KEY="explicit-key", DEBUG=True, _env_file=None)
    assert s.resolved_secret_key == "explicit-key"


def test_resolved_secret_key_dev_missing_generates_temporary_with_warning(monkeypatch, caplog):
    _clean_env(monkeypatch)
    s = Settings(SECRET_KEY="", DEBUG=True, _env_file=None)
    with caplog.at_level(logging.WARNING, logger="agentix.config.settings"):
        k1 = s.resolved_secret_key
        k2 = s.resolved_secret_key
    assert k1 and k2
    # 进程内必须稳定：签发/校验 JWT 各取一次，若每次随机则登录态必炸；
    # 「重启失效」语义由新进程重新生成来保证，而非每次访问重新生成。
    assert k1 == k2
    # 只告警一次（不随访问刷屏）
    warnings = [r for r in caplog.records if "SECRET_KEY not configured" in r.message]
    assert len(warnings) == 1


def test_resolved_secret_key_production_missing_raises(monkeypatch):
    _clean_env(monkeypatch)
    s = Settings(SECRET_KEY="", DEBUG=False, _env_file=None)
    with pytest.raises(RuntimeError, match="SECRET_KEY must be explicitly configured"):
        _ = s.resolved_secret_key


def test_settings_default_secret_key_is_empty(monkeypatch):
    _clean_env(monkeypatch)
    s = Settings(_env_file=None)
    assert s.SECRET_KEY == ""


# ---------------------------------------------------------------
# 2) multi_tool 工作流：error_tool_selection 注册 + 出边
# ---------------------------------------------------------------

@pytest.fixture(scope="module")
def compiled_workflow():
    from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.multi_tool import (
        create_multi_tool_workflow,
    )

    return create_multi_tool_workflow(
        llm=MagicMock(),
        graph=MagicMock(),
        tool_schemas=[],
        predefined_cypher_dict={},
        cypher_example_retriever=MagicMock(),
    )


def test_error_tool_selection_node_registered(compiled_workflow):
    g = compiled_workflow.get_graph()
    nodes = set(g.nodes.keys())
    assert "error_tool_selection" in nodes, (
        "error_tool_selection 未注册：tool_selection goto 到未注册节点会在 LangGraph 运行时崩溃"
    )


def test_error_tool_selection_has_outgoing_edge_to_summarize(compiled_workflow):
    g = compiled_workflow.get_graph()
    edges = {(e.source, e.target) for e in g.edges}
    assert ("error_tool_selection", "summarize") in edges, (
        "error_tool_selection 缺少到 summarize 的显式出边：非 Command 节点无出边会导致图执行挂起"
    )


def test_workflow_signature_accepts_documented_params():
    from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.multi_tool import (
        create_multi_tool_workflow,
    )

    sig = inspect.signature(create_multi_tool_workflow)
    assert "default_to_text2cypher" in sig.parameters
