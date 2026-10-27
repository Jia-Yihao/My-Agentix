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


def _compiled_destinations(compiled, node_name: str) -> set:
    """从**编译后的路由表**读一个节点的真实出边目标（而非 ``get_graph()`` 的画法）。

    LG-0-2（langgraph 0.2.60 → 1.2.18）实测出来的口径变更：1.2 线的 ``get_graph()``
    **不再画出「静态无入边」节点的出边**。``error_tool_selection`` 恰好是这种节点——
    它只被 ``tool_selection`` 在运行期以 ``Command(goto="error_tool_selection")``
    命中，而该目标**没有**写进 ``tool_selection`` 的 ``Command[Literal[...]]`` 注解
    ⇒ 画法上它的整条链都不出现（0.2 线会画出来）。

    但运行期照走：最小复现（goto 一个注解外的目标，该目标另有 ``add_edge`` 出边）
    读数 ``{'v': 1011}`` = 1 + 10 + 1000，证明仍沿显式边收敛。所以本用例的不变量
    （「兜底节点必须有到 summarize 的出边，否则图执行挂起」）**不依赖画法**，
    直接读路由表：``add_edge(a, b)`` 在编译期落为 a 的
    ``ChannelWriteEntry(channel="branch:to:b")``。
    """
    out: set = set()
    steps = getattr(compiled.nodes[node_name].node, "steps", []) or []
    for step in steps:
        for entry in getattr(step, "writes", ()) or ():
            channel = getattr(entry, "channel", None)
            if isinstance(channel, str) and channel.startswith("branch:to:"):
                out.add(channel[len("branch:to:") :])
    return out


def test_error_tool_selection_has_outgoing_edge_to_summarize(compiled_workflow):
    destinations = _compiled_destinations(compiled_workflow, "error_tool_selection")
    assert "summarize" in destinations, (
        "error_tool_selection 缺少到 summarize 的显式出边：非 Command 节点无出边会导致图执行挂起"
        f"（实测编译路由表出边目标：{sorted(destinations)}）"
    )


def test_workflow_signature_accepts_documented_params():
    from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.multi_tool import (
        create_multi_tool_workflow,
    )

    sig = inspect.signature(create_multi_tool_workflow)
    assert "default_to_text2cypher" in sig.parameters
