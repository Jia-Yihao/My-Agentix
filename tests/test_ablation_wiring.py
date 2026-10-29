"""消融开关接线验收测试（全离线，无 LLM / Redis / Neo4j / Docker）。

覆盖三件事：

1. **读取端语义** —— ``agentix.config.ablation`` 解析 ``AGENTIX_ABLATION_*``
   的默认值、显式覆盖、非法取值、未接线识别。
2. **接线真的生效** —— 五个已接线开关（retrieval / router / tools / memory /
   multi_agent）在被设为非默认值时，确实改变了对应生产节点的行为。
   这是本文件的核心价值：接线之前，这套环境变量「只写不读」，
   七个消融变体跑出来必然逐位相同，却会以「该能力无增益」的名义进表。
3. **契约守卫** —— ``run_ablation.check_variant`` 必须拒绝声明了未接线开关
   的变体（``multi_agent`` 已于 Phase 19 接线、当前 UNWIRED 为空，守卫
   机制用合成未接线项保持覆盖），避免「没有实现」被伪装成「实验结论」。
"""

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agentix.application.agents import general_handler, research_handler, router as router_module
from agentix.orchestration.langgraph.lg_states import AgentState, Router
from agentix.config import ablation
from agentix.config import settings as app_settings

ABLATION_FLAG_NAMES = tuple(ablation.FLAG_DEFAULTS)


@pytest.fixture(autouse=True)
def _clear_ablation_env(monkeypatch):
    """每个用例从「无任何消融环境变量」的干净状态出发。"""
    for name in ABLATION_FLAG_NAMES:
        monkeypatch.delenv(f"{ablation.ENV_PREFIX}{name.upper()}", raising=False)


def _set(monkeypatch, name: str, value: bool) -> None:
    monkeypatch.setenv(
        f"{ablation.ENV_PREFIX}{name.upper()}", str(bool(value)).lower()
    )


class _CapturingModel:
    """记录入参并返回固定答复的假 chat model。"""

    def __init__(self, content: str = "这是模型的直接作答。"):
        self.calls = []
        self._content = content

    async def ainvoke(self, messages, config=None, **kwargs):
        self.calls.append(messages)
        return AIMessage(content=self._content)


def _state(question: str = "民法典怎么规定违约金？") -> AgentState:
    return AgentState(messages=[HumanMessage(content=question)])


# ---------------------------------------------------------------------------
# 1. 读取端语义
# ---------------------------------------------------------------------------


class TestFlagLoading:
    def test_no_env_yields_production_defaults(self):
        """不注入任何变量 ⇒ 取值 = 生产真实现状（对现网零影响）。"""
        flags = ablation.load_ablation_flags({})

        assert dict(flags.values) == dict(ablation.FLAG_DEFAULTS)
        assert flags.explicit_overrides == {}
        assert flags.unwired_overrides == {}
        assert flags.invalid == {}
        assert flags.is_effectively_default is True
        assert flags.describe() == "(all defaults)"

    def test_explicit_override_is_recorded(self):
        flags = ablation.load_ablation_flags(
            {f"{ablation.ENV_PREFIX}RETRIEVAL_ENABLED": "false"}
        )
        assert flags.get("retrieval_enabled") is False
        assert flags.explicit_overrides == {"retrieval_enabled": False}
        assert flags.wired_overrides == {"retrieval_enabled": False}

    def test_value_equal_to_default_is_not_an_override(self):
        """显式写成默认值不算覆盖——这样变体 A–D 的 `multi_agent: false` 不会误报。"""
        flags = ablation.load_ablation_flags(
            {f"{ablation.ENV_PREFIX}MULTI_AGENT": "false"}
        )
        assert flags.explicit_overrides == {}
        assert flags.unwired_overrides == {}
        assert flags.is_effectively_default is True

    def test_unwired_flag_is_detected(self, monkeypatch):
        """未接线识别机制保持覆盖（当前 UNWIRED 为空 ⇒ 注入合成项验证）。"""
        monkeypatch.setitem(
            ablation.UNWIRED_FLAGS, "memory_enabled", "synthetic-unwired"
        )
        flags = ablation.load_ablation_flags(
            {f"{ablation.ENV_PREFIX}MEMORY_ENABLED": "true"}
        )
        assert flags.unwired_overrides == {"memory_enabled": True}
        assert flags.is_effectively_default is False

    def test_multi_agent_is_wired_with_file_line(self):
        """Phase 19：multi_agent 已 UNWIRED → WIRED，接线点可定位。"""
        assert "multi_agent" in ablation.WIRED_FLAGS
        assert "multi_agent" not in ablation.UNWIRED_FLAGS
        point = ablation.WIRED_FLAGS["multi_agent"]
        assert "lg_builder.py::get_active_graph" in point
        assert "lg_supervisor.py::supervisor_dispatch" in point

    @pytest.mark.parametrize("raw", ["maybe", "", "  ", "2", "yes-ish"])
    def test_invalid_value_falls_back_without_raising(self, raw):
        """一个写错的环境变量不该把整条问答链路打挂。"""
        flags = ablation.load_ablation_flags(
            {f"{ablation.ENV_PREFIX}ROUTER_ENABLED": raw}
        )
        assert flags.get("router_enabled") is ablation.FLAG_DEFAULTS["router_enabled"]
        assert flags.invalid == {"router_enabled": raw}

    def test_unknown_flag_name_is_recorded_not_applied(self):
        flags = ablation.load_ablation_flags(
            {f"{ablation.ENV_PREFIX}NOT_A_FLAG": "true"}
        )
        assert "not_a_flag" not in flags.values
        assert flags.invalid == {"not_a_flag": "true"}

    def test_other_env_vars_are_ignored(self):
        flags = ablation.load_ablation_flags(
            {"PATH": "/usr/bin", "AGENTIX_SOMETHING_ELSE": "1"}
        )
        assert flags.explicit_overrides == {}
        assert flags.invalid == {}

    def test_wired_and_unwired_partition_is_disjoint(self):
        assert not set(ablation.WIRED_FLAGS) & set(ablation.UNWIRED_FLAGS)
        assert set(ablation.WIRED_FLAGS) | set(ablation.UNWIRED_FLAGS) | set(
            ablation.INFORMATIONAL_FLAGS
        ) == set(ablation.FLAG_DEFAULTS)


# ---------------------------------------------------------------------------
# 2. 接线真的生效
# ---------------------------------------------------------------------------


class TestRouterAblation:
    @pytest.mark.asyncio
    async def test_disabled_skips_llm_classification_entirely(self, monkeypatch):
        """router 关掉时不得构造分类 LLM——否则「绕过路由」名不副实。"""

        def _boom(**kwargs):
            raise AssertionError("router_enabled=False 时不得请求分类 LLM")

        monkeypatch.setattr(router_module, "get_llm_client", _boom)
        _set(monkeypatch, "router_enabled", False)

        result = await router_module.analyze_and_route_query(_state(), config={})

        assert result["router"].type == "kb-query"
        assert "ablation" in result["router"].logic

    @pytest.mark.asyncio
    async def test_enabled_still_calls_llm_classification(self, monkeypatch):
        """默认（开关不动）必须保持原行为：仍然走分类 LLM。"""
        model = _CapturingModel()

        class _Structured:
            async def ainvoke(self, messages):
                return Router(type="graphrag-query", logic="llm")

        monkeypatch.setattr(
            router_module, "get_llm_client", lambda **kw: _with_structured(model)
        )

        result = await router_module.analyze_and_route_query(_state(), config={})
        assert result["router"].type == "graphrag-query"

    def test_route_query_forced_to_kb_when_disabled(self, monkeypatch):
        _set(monkeypatch, "router_enabled", False)
        state = _state()
        state.router = Router(type="graphrag-query", logic="llm")

        assert router_module.route_query(state) == "create_kb_query"

    def test_route_query_dispatches_normally_when_enabled(self):
        state = _state()
        state.router = Router(type="graphrag-query", logic="llm")

        assert router_module.route_query(state) == "create_research_plan"

    def test_explicit_image_hint_outranks_ablation_switch(self, monkeypatch):
        """image_path/file_path 是调用方给定的事实，不是模型路由决策，优先于开关。"""
        _set(monkeypatch, "router_enabled", False)
        state = _state()
        state.config = {"configurable": {"image_path": "/tmp/a.png"}}

        assert router_module.route_query(state) == "create_image_query"


class TestRetrievalAblation:
    @pytest.mark.asyncio
    async def test_disabled_answers_with_llm_without_touching_retrieval(
        self, monkeypatch
    ):
        model = _CapturingModel("没有检索，直接作答。")
        monkeypatch.setattr(general_handler, "get_llm_client", lambda **kw: model)
        _set(monkeypatch, "retrieval_enabled", False)

        result = await general_handler.create_kb_query(_state(), config={})

        assert model.calls, "应走 LLM 直答"
        assert result["messages"][0].content == "没有检索，直接作答。"
        # 有意行为，不是系统故障：不能打 degraded，否则污染评测里的降级计数
        assert not result["messages"][0].additional_kwargs.get("degraded")
        assert "sources" not in result

    @pytest.mark.asyncio
    async def test_disabled_does_not_build_retrieval_workflow(self, monkeypatch):
        """确认没有构造 KnowledgeGateway（否则就不是「无检索」）。"""
        created = []

        class _SpyGateway:
            def __init__(self, *args, **kwargs):
                created.append("gateway")

        monkeypatch.setattr(general_handler, "KnowledgeGateway", _SpyGateway)
        monkeypatch.setattr(
            general_handler, "get_llm_client", lambda **kw: _CapturingModel()
        )
        _set(monkeypatch, "retrieval_enabled", False)

        await general_handler.create_kb_query(_state(), config={})
        assert created == []


class TestToolsAblation:
    @pytest.mark.asyncio
    async def test_disabled_skips_kg_subgraph(self, monkeypatch):
        """tools 关掉时不得进入 KG 多工具子图，也不该因缺 API key 而崩。"""
        model = _CapturingModel("没有工具，直接作答。")
        monkeypatch.setattr(general_handler, "get_llm_client", lambda **kw: model)
        monkeypatch.setattr(app_settings, "LLM_API_KEY", "")
        monkeypatch.setattr(
            research_handler,
            "_get_compiled_multi_tool_workflow",
            lambda **kw: pytest.fail("tools_enabled=False 时不得编译 KG 多工具子图"),
        )
        _set(monkeypatch, "tools_enabled", False)

        result = await research_handler.create_research_plan(_state(), config={})
        assert result["messages"][0].content == "没有工具，直接作答。"

    @pytest.mark.asyncio
    async def test_enabled_still_requires_api_key(self, monkeypatch):
        """默认路径保持原有前置校验（不得被消融分支无意放宽）。"""
        monkeypatch.setattr(app_settings, "LLM_API_KEY", "")
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            await research_handler.create_research_plan(_state(), config={})


class TestMemoryAblation:
    @pytest.mark.asyncio
    async def test_flag_opens_recall_beyond_settings(self, monkeypatch):
        """生产默认 settings 关闭记忆；消融开关要能把它打开（供 F_memory / G_full）。"""
        from agentix.application.services import chat_service
        from agentix.application.services import long_term_memory as ltm

        monkeypatch.setattr(app_settings, "MEMORY_CONTEXT_ENABLED", False)
        monkeypatch.setattr(app_settings, "MEMORY_RECALL_LIMIT", 3)

        class _FakeLTM:
            async def get_context_for_query(self, query, *, max_summaries=3):
                return "历史摘要：此前咨询过违约金"

        monkeypatch.setattr(ltm, "get_long_term_memory", lambda: _FakeLTM())
        _set(monkeypatch, "memory_enabled", True)

        assert "违约金" in await chat_service._maybe_memory_context("违约金怎么算")

    @pytest.mark.asyncio
    async def test_both_off_short_circuits(self, monkeypatch):
        from agentix.application.services import chat_service
        from agentix.application.services import long_term_memory as ltm

        monkeypatch.setattr(app_settings, "MEMORY_CONTEXT_ENABLED", False)
        monkeypatch.setattr(
            ltm,
            "get_long_term_memory",
            lambda: pytest.fail("两个开关都关时不得触达长期记忆服务"),
        )

        assert await chat_service._maybe_memory_context("违约金怎么算") == ""


# ---------------------------------------------------------------------------
# 3. 契约守卫
# ---------------------------------------------------------------------------


class TestVariantContractGuard:
    @staticmethod
    def _variants():
        path = Path("evals/ablations/variants.json")
        return json.loads(path.read_text(encoding="utf-8"))["variants"]

    def test_every_declared_flag_is_a_known_flag(self):
        """variants.json 里不得出现拼错的开关名（拼错会被静默忽略）。"""
        known = set(ablation.FLAG_DEFAULTS)
        for variant in self._variants():
            unknown = set(variant.get("flags", {})) - known
            assert not unknown, f"{variant['id']} 声明了未知开关: {unknown}"

    @pytest.mark.parametrize("variant_id", ["A_llm_only", "B_rag", "C_rag_router", "D_tools"])
    def test_ablation_variants_are_executable(self, variant_id):
        """A–D 只用已接线开关 ⇒ 必须放行。"""
        from evals.ablations.run_ablation import check_variant

        variant = next(v for v in self._variants() if v["id"] == variant_id)
        blocking, _notes = check_variant(variant)
        assert blocking == [], f"{variant_id} 不应被拦截: {blocking}"

    @pytest.mark.parametrize("variant_id", ["E_multi_agent", "F_memory", "G_full"])
    def test_variants_requiring_multi_agent_are_executable(self, variant_id):
        """Phase 19：multi_agent 已接线 ⇒ E/F/G 必须放行，且备注指出接线点。"""
        from evals.ablations.run_ablation import check_variant

        variant = next(v for v in self._variants() if v["id"] == variant_id)
        blocking, notes = check_variant(variant)
        assert blocking == [], f"{variant_id} 不应被拦截: {blocking}"
        assert any("multi_agent" in note for note in notes)

    @pytest.mark.parametrize(
        "variant_id", ["A_llm_only", "B_rag", "C_rag_router", "D_tools",
                       "E_multi_agent", "F_memory", "G_full"]
    )
    def test_all_variants_pass_contract_guard(self, variant_id):
        """七变体全部只用已接线/信息性开关 ⇒ 契约守卫全放行。"""
        from evals.ablations.run_ablation import check_variant

        variant = next(v for v in self._variants() if v["id"] == variant_id)
        blocking, _notes = check_variant(variant)
        assert blocking == [], f"{variant_id} 不应被拦截: {blocking}"

    def test_allow_unwired_escape_hatch(self, capsys, monkeypatch):
        """--allow-unwired 是逃生舱：仍返回可执行，但把风险打在输出里。

        multi_agent 已接线（UNWIRED 为空）⇒ 注入合成未接线项触发 BLOCK 路径。
        """
        from evals.ablations.run_ablation import validate_variants

        monkeypatch.setitem(
            ablation.UNWIRED_FLAGS, "memory_enabled", "synthetic-unwired"
        )
        blocked = validate_variants(self._variants(), allow_unwired=True)
        assert blocked == []
        assert "BLOCK" in capsys.readouterr().out

    def test_main_refuses_and_exits_2(self, capsys, monkeypatch):
        """契约守卫拒绝路径：变体声明了（合成）未接线开关 ⇒ 退出码 2。"""
        from evals.ablations.run_ablation import main

        monkeypatch.setitem(
            ablation.UNWIRED_FLAGS, "memory_enabled", "synthetic-unwired"
        )
        code = main(["--variant", "F_memory"])
        assert code == 2
        assert "拒绝执行" in capsys.readouterr().out

    def test_check_variant_flags_unknown_flag_as_blocking(self):
        from evals.ablations.run_ablation import check_variant

        blocking, notes = check_variant(
            {"id": "X", "flags": {"retrieval_enabled": False, "svm_enabled": True}}
        )
        assert any("svm_enabled" in reason for reason in blocking)
        assert any("retrieval_enabled" in note for note in notes)


def _with_structured(model):
    """给假 model 挂一个 with_structured_output 返回自身（模拟结构化输出链）。"""

    class _Structured:
        async def ainvoke(self, messages):
            return Router(type="graphrag-query", logic="llm")

    def _factory(*args, **kwargs):
        return _Structured()

    model.with_structured_output = _factory
    return model
