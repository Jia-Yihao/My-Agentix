"""P2（岗位对照·动态调度组合）：tool_policy 注入方 + 延迟工具召回。

把 P2 的 DoD 逐条钉死：

1. **flag 治理**：``TOOL_SELECTION_ENABLED`` / ``TOOL_SELECTION_MAX_RISK`` /
   ``TOOL_DEFERRED_TOOLS`` / ``TOOL_RESULT_CONTRACT_ENABLED`` 默认关/空，
   默认配置下声明清单与基线**逐位等价**（无 tool_search、无裁剪）；
2. **双层闸 L1 注入方**（2.4/2.5）：``build_tool_policy`` flag-off 返 None、
   flag-on 出策略、非法 max_risk 降级不抛；``_inject_tool_policy`` flag-off
   **不触碰 config**；ask 与 stream 两条图路径都接了注入（源码计数守卫）；
3. **延迟工具面**（2.2）：``TOOL_DEFERRED_TOOLS`` 非空 ⇒ 常驻 schema 剔除、
   注册表 spec 保留、``tool_search`` 声明+装配；``tool_search`` 自身不可被
   延迟（机制自锁=配置笔误）；召回结果**含延迟工具**且带入参 schema；
4. **Tool Ranking 层**（2.2）：``score_tool_match`` / ``rank_tool_schemas``
   确定性、CJK bigram 匹配、top_k 稳定截断。

全局单例（声明缓存 / executor / 注册表）在夹具里成对复位，
与 Phase 21/23/P1 测试同款范式。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from agentix.application.services.chat_service import _inject_tool_policy
from agentix.tools import registry_init
from agentix.tools.definitions import ToolSpec, registry as tool_registry
from agentix.tools.gateway.executor import (
    get_tool_executor,
    reset_tool_executor,
)
from agentix.tools.selection import (
    ToolSelectionPolicy,
    build_tool_policy,
    rank_tool_schemas,
    score_tool_match,
    select_tool_schemas,
)
from agentix.tools.adapters.tool_search import (
    build_tool_search_spec,
    install_tool_search_tool,
    search_declarations,
)
from agentix.config.settings import settings

_CHAT_SERVICE_SRC = (
    Path(__file__).resolve().parents[1]
    / "agentix"
    / "application"
    / "services"
    / "chat_service.py"
).read_text(encoding="utf-8")


def _clean_decl_state() -> None:
    """清声明缓存 + 摘掉 P2 测试挂进全局 registry 的 tool_search spec。"""
    registry_init._DECLARATIONS.clear()
    tool_registry.delete_tool("tool_search")


def _titles() -> list[str]:
    return [s.model_json_schema().get("title", "") for s in registry_init.get_active_tools()]


@pytest.fixture
def flags_off(monkeypatch: pytest.MonkeyPatch):
    """默认 flag 态 + 相邻 flag 隔离（声明清单与基线逐位等价）。"""
    for key in (
        "SKILLS_ENABLED",
        "NOTIFY_USER_ENABLED",
        "TASK_EXEC_ENABLED",
        "TOOL_SELECTION_ENABLED",
        "TOOL_RESULT_CONTRACT_ENABLED",
    ):
        monkeypatch.setattr(settings, key, False)
    monkeypatch.setattr(settings, "TOOL_DEFERRED_TOOLS", "")
    monkeypatch.setattr(settings, "DISABLED_TOOLS", "")
    _clean_decl_state()
    reset_tool_executor()
    yield
    _clean_decl_state()
    reset_tool_executor()


@pytest.fixture
def deferred_on(monkeypatch: pytest.MonkeyPatch):
    """延迟列表非空态：声明缓存/executor/注册表三单例复位 + 双向清理。"""
    for key in (
        "SKILLS_ENABLED",
        "NOTIFY_USER_ENABLED",
        "TASK_EXEC_ENABLED",
        "TOOL_SELECTION_ENABLED",
        "TOOL_RESULT_CONTRACT_ENABLED",
    ):
        monkeypatch.setattr(settings, key, False)
    monkeypatch.setattr(settings, "DISABLED_TOOLS", "")
    monkeypatch.setattr(settings, "TOOL_DEFERRED_TOOLS", "cypher_query")
    _clean_decl_state()
    reset_tool_executor()
    yield
    _clean_decl_state()
    reset_tool_executor()


# ---------------------------------------------------------------------------
# 1. flag 治理（默认关/空）
# ---------------------------------------------------------------------------


class TestFlagGovernance:
    def test_defaults_are_off_and_baseline_equal(self, flags_off):
        assert settings.TOOL_SELECTION_ENABLED is False
        assert settings.TOOL_SELECTION_MAX_RISK == "L1"
        assert settings.TOOL_DEFERRED_TOOLS == ""
        assert settings.TOOL_RESULT_CONTRACT_ENABLED is False
        # 默认声明清单 = 基线 4 个 KG 工具，无 tool_search、无裁剪
        assert _titles() == [
            "cypher_query",
            "predefined_cypher",
            "microsoft_graphrag_query",
            "text2sql_query",
        ]

    def test_build_tool_policy_flag_off_returns_none(self, flags_off):
        assert build_tool_policy() is None

    def test_inject_helper_flag_off_does_not_touch_config(self, flags_off):
        config = {"configurable": {"thread_id": "t1"}}
        snapshot = dict(config)
        _inject_tool_policy(config)
        assert config == snapshot
        assert "tool_policy" not in config["configurable"]


# ---------------------------------------------------------------------------
# 2. 双层闸 L1 注入方（2.4/2.5）
# ---------------------------------------------------------------------------


class TestToolPolicyInjection:
    def test_build_policy_flag_on_reads_max_risk(self, flags_off, monkeypatch):
        monkeypatch.setattr(settings, "TOOL_SELECTION_ENABLED", True)
        monkeypatch.setattr(settings, "TOOL_SELECTION_MAX_RISK", "L2")
        policy = build_tool_policy()
        assert isinstance(policy, ToolSelectionPolicy)
        assert policy.max_risk_level == "L2"

    def test_invalid_max_risk_degrades_to_none_without_raise(
        self, flags_off, monkeypatch
    ):
        monkeypatch.setattr(settings, "TOOL_SELECTION_ENABLED", True)
        monkeypatch.setattr(settings, "TOOL_SELECTION_MAX_RISK", "L9")
        assert build_tool_policy() is None  # 记 error、不抛、不注入

    def test_inject_helper_flag_on_adds_policy(self, flags_off, monkeypatch):
        monkeypatch.setattr(settings, "TOOL_SELECTION_ENABLED", True)
        config = {"configurable": {"thread_id": "t1"}}
        _inject_tool_policy(config)
        policy = config["configurable"]["tool_policy"]
        assert isinstance(policy, ToolSelectionPolicy)
        assert policy.max_risk_level == "L1"  # 默认上限
        assert config["configurable"]["thread_id"] == "t1"  # 原键不动

    def test_both_graph_paths_wire_the_injector(self):
        """ask 与 stream 两条图配置构建都必须接注入方（源码计数守卫）。"""
        assert _CHAT_SERVICE_SRC.count("_inject_tool_policy(config)") == 2

    def test_injected_policy_actually_filters_visibility(self, flags_off, monkeypatch):
        """注入 → 过滤的端到端：L2 声明在 L1 策略下从可见面消失。"""

        class p51_danger(BaseModel):
            """需要人工确认的写操作工具"""

        spec = ToolSpec(
            name="p51_danger",
            description="危险工具",
            risk_level="L2",
            control="需人工审批（HITL）",
        )
        if not tool_registry.has_tool("p51_danger"):
            tool_registry.register(spec, lambda: None)
        try:
            monkeypatch.setattr(settings, "TOOL_SELECTION_ENABLED", True)
            policy = build_tool_policy()
            assert policy is not None
            assert select_tool_schemas([p51_danger], policy) == []  # L2 > L1 上限
            # 放宽上限到 L2 后重新可见（证明被过滤的是风险级而非其他维度）
            assert select_tool_schemas(
                [p51_danger], ToolSelectionPolicy(max_risk_level="L2")
            ) == [p51_danger]
        finally:
            tool_registry.delete_tool("p51_danger")


# ---------------------------------------------------------------------------
# 3. 延迟工具面（2.2）：常驻裁剪 + tool_search 召回
# ---------------------------------------------------------------------------


class TestDeferredResidentSplit:
    def test_deferred_tool_leaves_resident_but_keeps_spec(self, deferred_on):
        titles = _titles()
        assert "cypher_query" not in titles  # 退出常驻
        assert "tool_search" in titles  # 召回工具挂进声明（延迟非空）
        assert {"predefined_cypher", "microsoft_graphrag_query", "text2sql_query"} <= set(
            titles
        )
        registry_init.ensure_registered()
        assert tool_registry.has_tool("cypher_query")  # spec 永不删，可随时恢复
        declared = {spec.name for spec, _ in registry_init.get_declarations()}
        assert "cypher_query" in declared  # 召回面仍含延迟工具

    def test_disabled_and_deferred_compose(self, deferred_on, monkeypatch):
        monkeypatch.setattr(settings, "DISABLED_TOOLS", "text2sql_query")
        titles = set(_titles())
        assert "text2sql_query" not in titles  # 禁用
        assert "cypher_query" not in titles  # 延迟
        assert "tool_search" in titles

    def test_tool_search_itself_never_deferrable(self, flags_off, monkeypatch):
        monkeypatch.setattr(settings, "TOOL_DEFERRED_TOOLS", "tool_search")
        assert registry_init.deferred_tool_names() == frozenset()
        assert "tool_search" not in _titles()  # 机制不自锁：不声明

    def test_spec_declaration_gated_by_empty_list(self, flags_off):
        # 空列表 ⇒ tool_search 不进声明（install 也 no-op，见下）
        assert "tool_search" not in _titles()
        install_tool_search_tool()
        assert not get_tool_executor().has_handler("tool_search")


class TestToolSearchRecall:
    @pytest.fixture
    def search_ready(self, deferred_on):
        install_tool_search_tool()
        yield
        reset_tool_executor()

    @pytest.mark.asyncio
    async def test_recall_contains_deferred_tool_with_schema(self, search_ready):
        executor = get_tool_executor()
        assert executor.has_handler("tool_search")
        result = await executor.execute("tool_search", {"query": "cypher 法条"})
        assert result.success, result.error
        names = [r["name"] for r in result.data["results"]]
        assert "cypher_query" in names  # 召回的核心价值：延迟工具找得回
        hit = next(r for r in result.data["results"] if r["name"] == "cypher_query")
        assert hit["deferred"] is True
        assert hit["risk_level"] == "L0"
        assert hit["schema"]["title"] == "cypher_query"  # 入参签名可直接给 LLM
        # 常驻面里没有它（分拆成立）
        assert "cypher_query" not in _titles()

    @pytest.mark.asyncio
    async def test_recall_is_deterministic_and_respects_top_k(self, search_ready):
        executor = get_tool_executor()
        first = await executor.execute("tool_search", {"query": "法条查询", "top_k": 2})
        second = await executor.execute("tool_search", {"query": "法条查询", "top_k": 2})
        assert first.success and second.success
        assert first.data == second.data  # 确定性（排序稳定、无随机）
        assert first.data["count"] == 2

    @pytest.mark.asyncio
    async def test_empty_query_returns_catalog_prefix(self, search_ready):
        executor = get_tool_executor()
        result = await executor.execute("tool_search", {"query": ""})
        assert result.success
        assert result.data["count"] == len(registry_init.get_declarations())


# ---------------------------------------------------------------------------
# 4. Tool Ranking 层（2.2）
# ---------------------------------------------------------------------------


class _CalendarTool(BaseModel):
    """manage calendar events and reminders"""


class _WeatherTool(BaseModel):
    """query weather forecasts"""


class TestToolRanking:
    def test_score_prefers_name_and_description_hits(self):
        hit = score_tool_match("cypher_query", "用 Cypher 查询知识图谱", "cypher 查询")
        miss = score_tool_match("weather", "天气", "cypher 查询")
        assert hit > miss == 0

    def test_empty_query_scores_zero(self):
        assert score_tool_match("any", "任意描述", "") == 0.0

    def test_cjk_bigram_matching(self):
        # 中文查询靠 bigram 与中文 description 交集，不依赖英文词
        score = score_tool_match("unrelated", "查询法律法规条文", "法规查询")
        assert score > 0

    def test_rank_orders_and_truncates_stably(self):
        ranked = rank_tool_schemas(
            [_CalendarTool, _WeatherTool], "calendar reminders", top_k=8
        )
        assert ranked[0] is _CalendarTool  # 命中排前
        assert ranked[1] is _WeatherTool  # 未命中不丢（fail-open）
        assert rank_tool_schemas(
            [_CalendarTool, _WeatherTool], "calendar", top_k=1
        ) == [_CalendarTool]

    def test_rank_on_real_declarations(self, flags_off):
        active = registry_init.get_active_tools()
        ranked = rank_tool_schemas(active, "法条 引用 关系", top_k=3)
        assert len(ranked) == 3
        assert ranked == rank_tool_schemas(active, "法条 引用 关系", top_k=3)  # 确定性

    def test_spec_builder_is_l0_with_discovery_capability(self):
        spec = build_tool_search_spec()
        assert spec.name == "tool_search"
        assert spec.risk_level == "L0"
        assert "discovery" in spec.capabilities
        assert spec.parameters["title"] == "tool_search"


class TestSearchDeclarationsUnit:
    def test_search_covers_all_declarations_even_when_deferred(self, deferred_on):
        payload = search_declarations("cypher", top_k=5)
        names = [r["name"] for r in payload["results"]]
        assert "cypher_query" in names
        assert payload["count"] <= 5

    def test_search_rejects_nothing_on_garbage_query(self, deferred_on):
        # 垃圾查询：不抛、不静默丢工具——按（分数, 声明序）稳定返回
        payload = search_declarations("zzz-不存在的查询zzz", top_k=2)
        assert payload["count"] == 2
        declared_order = [spec.name for spec, _ in registry_init.get_declarations()]
        assert payload["results"][0]["name"] == declared_order[0]
        assert payload["results"][1]["name"] == declared_order[1]
