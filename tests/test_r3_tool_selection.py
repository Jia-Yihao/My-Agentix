"""R3-4：工具选择过滤链测试（架构文档 §17）。

核心原则：LLM 不应该看到它没有权限使用的 Tool——
risk（max_risk_level）→ 白/黑名单 → capabilities 三层过滤；
消费点 tool_selection 节点（configurable["tool_policy"]，缺省零变化）。
"""

from __future__ import annotations

from typing import Optional

import pytest
from pydantic import BaseModel

from agentix.tools.definitions import ToolSpec, registry as tool_registry
from agentix.tools.selection import ToolSelectionPolicy, select_tool_schemas


class DummyAlpha(BaseModel):
    """title=DummyAlpha（schema title 与 spec 名一致是回查约定）。"""

    question: str
    unused: Optional[str] = None


class DummyBeta(BaseModel):
    question: str


class DummyGamma(BaseModel):
    question: str


def _register_test_tools() -> None:
    """注册测试工具（幂等）：alpha L0 / beta L2 / gamma L0+capability。"""
    if tool_registry.has_tool("DummyAlpha"):
        return
    tool_registry.register(
        ToolSpec(name="DummyAlpha", description="d", risk_level="L0",
                capabilities=("retrieval",)),
        lambda: None,
    )
    tool_registry.register(
        ToolSpec(name="DummyBeta", description="d", risk_level="L2",
                capabilities=("write",),
                # L2 必须声明 control（§2.1.5⑤，ToolSpec 构造期强制校验）
                side_effect=True, control="需人工审批（HITL）"),
        lambda: None,
    )
    tool_registry.register(
        ToolSpec(name="DummyGamma", description="d", risk_level="L0",
                capabilities=("sql",)),
        lambda: None,
    )


@pytest.fixture(autouse=True)
def _tools():
    """注册测试工具并在用例结束后恢复全局注册表——防止 Dummy* 泄漏进
    后续用例（如 test_r1_tool_risk_levels 的"全部 L0"断言会被 L2 夹具打红）。"""
    _register_test_tools()
    yield
    for name in ("DummyAlpha", "DummyBeta", "DummyGamma"):
        tool_registry.delete_tool(name)


_ALL = [DummyAlpha, DummyBeta, DummyGamma]
_ALL_NAMES = {"DummyAlpha", "DummyBeta", "DummyGamma"}


def _names(schemas):
    return {s.model_json_schema()["title"] for s in schemas}


# ---------------------------------------------------------------- 基线


def test_no_spec_schema_fail_open_kept():
    """无 spec 的 schema 默认保留（fail-open，声明缺失不误杀）。"""

    class DummyOrphan(BaseModel):
        question: str

    kept = select_tool_schemas([DummyOrphan], ToolSelectionPolicy(max_risk_level="L1"))
    assert _names(kept) == {"DummyOrphan"}


def test_policy_invalid_risk_level_rejected():
    with pytest.raises(ValueError):
        ToolSelectionPolicy(max_risk_level="L9")


# ---------------------------------------------------------------- 风险闸门


def test_risk_filter_hides_l2_from_l1_policy():
    kept = select_tool_schemas(_ALL, ToolSelectionPolicy(max_risk_level="L1"))
    assert _names(kept) == {"DummyAlpha", "DummyGamma"}


def test_risk_filter_l2_visible_to_l2_policy():
    kept = select_tool_schemas(_ALL, ToolSelectionPolicy(max_risk_level="L2"))
    assert _names(kept) == _ALL_NAMES


# ---------------------------------------------------------------- 白/黑名单


def test_whitelist_filters_others():
    policy = ToolSelectionPolicy(max_risk_level="L2", allowed_tools=frozenset({"DummyAlpha"}))
    assert _names(select_tool_schemas(_ALL, policy)) == {"DummyAlpha"}


def test_blacklist_hides_denied():
    policy = ToolSelectionPolicy(max_risk_level="L2", denied_tools=frozenset({"DummyBeta"}))
    assert _names(select_tool_schemas(_ALL, policy)) == {"DummyAlpha", "DummyGamma"}


def test_whitelist_wins_over_risk_rank():
    """白名单的工具仍受风险闸门约束（白名单 ∩ 风险，非并集）。"""
    policy = ToolSelectionPolicy(max_risk_level="L1", allowed_tools=frozenset({"DummyBeta"}))
    assert select_tool_schemas(_ALL, policy) == []


# ---------------------------------------------------------------- capabilities


def test_capability_filter_matches_intersection():
    policy = ToolSelectionPolicy(
        max_risk_level="L2", allowed_capabilities=frozenset({"sql", "write"})
    )
    # beta: write ∩ {sql,write} 命中；gamma: sql 命中；alpha: retrieval 不命中
    assert _names(select_tool_schemas(_ALL, policy)) == {"DummyBeta", "DummyGamma"}


def test_capability_filter_with_no_spec_schema_hidden():
    """能力受限请求下，无 spec（无能力标签）的 schema 不可见。"""

    class DummyOrphan(BaseModel):
        question: str

    policy = ToolSelectionPolicy(max_risk_level="L2", allowed_capabilities=frozenset({"sql"}))
    assert select_tool_schemas([DummyOrphan, DummyGamma], policy) == [DummyGamma]


def test_spec_override_bypasses_registry():
    """spec_overrides 优先于 registry（测试/扩展旁路）。"""
    spec = ToolSpec(name="DummyBeta", description="d", risk_level="L0")
    kept = select_tool_schemas(_ALL, ToolSelectionPolicy(max_risk_level="L1"), spec_overrides=[spec])
    assert _names(kept) == _ALL_NAMES


# ---------------------------------------------------------------- 节点接线守卫


def test_tool_selection_node_supports_policy_config():
    """接线守卫：节点接受 config 并按 tool_policy 选链（防接线被误删）。"""
    import inspect
    from unittest.mock import MagicMock

    from langchain_core.runnables import RunnableLambda

    from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.tool_selection import (
        node as ts_node,
    )

    source = inspect.getsource(ts_node)
    assert "tool_policy" in source
    assert "_chain_for_policy" in source

    llm = MagicMock()
    llm.bind_tools.return_value = RunnableLambda(lambda x: None)
    created = ts_node.create_tool_selection_node(llm=llm, tool_schemas=[DummyAlpha])
    params = inspect.signature(created).parameters
    assert "config" in params
