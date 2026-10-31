"""p176——T30-2「单层路由收敛为一层（flag-on 分支）」验收。

任务卡：``docs/design/审批与中断恢复优化规划.md`` §4.3 T30 卡 + 卡内 ⑦ 的
A1–A7 归属表（已裁决：**只动 A1（L1 意图分类）与 A3（KB 检索路由）**，
A2/A4a/A4b/A5/A7 保留原样，A6 未接线）。
口径：``agentix/config/settings.py::ROUTING_SINGLE_LAYER_ENABLED``（默认关）、
``.qoder/rules/task-order.md``（纪律 1 flag-off 逐位等价）。

覆盖面与验收口径
----------------

1. ``TestFlagOffEquivalence``——flag-off **逐位等价**（纪律 1）：
   ① ``_router_class(a, b, False)`` 与既有四类**同一对象**；
   ② ``_router_prompt_text(a, b, False)`` 与既有拼接结果**逐字节相同**，
      三项全关时逐字节等于 ``ROUTER_SYSTEM_PROMPT``；
   ③ ``_exec_flags()`` 是三项且默认全 False（新键字段默认值也钉死）；
   ④ 生产链路 flag-off 时结构化输出 schema 仍是基类 ``Router``，产出的
      router 上**没有** kb 参数。
2. ``TestGenerationToken``——换代令牌随 ``ROUTING_SINGLE_LAYER_ENABLED``
   翻动而变、翻回去与初始值相同（否则单层结论会被三种请求命中）。
3. ``TestSingleLayerProducesParams``（A1 产参）——四种 flag 组合映射到四个
   单层变体类（类名稳定）；提示词末尾追加新块且**基座一字节未动**；模型给出
   ``kb_route``/``kb_tools`` 时**必须带出到返回值**（不被最后一跳丢掉）；
   分派仍按 ``type`` 走（``route_type`` 词汇零改动）；兜底出口
   （异常 / fast-path）**一律不伪造**默认通道。
4. ``TestRouterNodeShortCircuit``（A3 消费）——state 里预置 ``route`` ⇒
   **0 次模型调用**，且**三段策略钳制照旧生效**（``allow_external_search``
   是安全边界，漏掉等于让 L1 越过 ``KB_ENABLE_EXTERNAL_SEARCH``）；
   无 ``route`` ⇒ 照常调一次模型（跳数回到基线）。
5. ``TestKbInputStateCarriesForwardedParams``——**本批最容易静默失效的一点**：
   KB 子图是 ``StateGraph(KBWorkflowState, input=KBInputState)``，输入面被
   ``KBInputState`` 过滤——若该 TypedDict 没有这两个键，转发的参数会被框架
   **悄悄丢掉**，A3 照样调模型、测试还全绿。故用**真实的两个 schema** 起一张
   单节点图，钉住"转发的键能到达节点"。
6. 第 6 节模块级用例 ``test_create_kb_query_*``——``create_kb_query`` 只在 L1 给出参数时
   才并入 ``route``/``kb_tools`` 两个输入键；**取不到时连键都不加**
   （断言键不存在，而不是断言值为 None）。
7. ``TestKnownCapabilityGap``——缓存字段集合不含 kb 参数（**本批刻意不扩**，
   那会改 T8 的缓存语义）：同时开 ``ROUTING_CACHE_ENABLED`` 时命中请求拿不到
   单层参数，A3 自己再调一次模型（跳数 = 基线，不省但不错）。

全部用例离线可跑：无真实 LLM（一律桩），无 Redis/Postgres/Milvus/Neo4j。
复验：``.venv/Scripts/python.exe -m pytest tests/test_p176_routing_single_layer.py -q``

已知边界（未钉住的面，不要读成"已验证"）
----------------------------------------

1. **端到端"省了一跳"只在节点/入参两层钉**，没有跑真实 KB 子图全图
   （要拉 KnowledgeGateway 与检索后端）；全图跳数属 T30-3 的基准对照面。
2. **flag-off 图拓扑不变**靠"本批不改 ``lg_builder``/``lg_supervisor``/
   ``edges``"这一改动边界保证（git 可核），本文件不重复断言拓扑。
3. 单层变体的 ``kb_route``/``kb_tools`` 取值域由 pydantic ``Literal`` 固化，
   本文件不重述 schema JSON 结构（结构化输出面由 p140/p49 的守卫族覆盖）。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, START
from langgraph.graph.state import StateGraph

import agentix.application.agents.general_handler as general_handler_mod
import agentix.application.agents.router as router_mod
from agentix.application.agents import routing_cache
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent import (
    multi_tool as multi_tool_mod,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_nodes.router_node import (
    create_router_node,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_states import (
    KBInputState,
    KBRouteDecision,
    KBWorkflowState,
)
from agentix.application.agents.lg_prompts import (
    CODE_EXEC_ROUTE_BLOCK,
    ROUTER_SYSTEM_PROMPT,
    SINGLE_LAYER_ROUTE_BLOCK,
    TASK_EXEC_ROUTE_BLOCK,
)
from agentix.config import ablation, settings
from agentix.orchestration.langgraph.lg_states import (
    AgentState,
    CodeRouter,
    CodeSingleLayerRouter,
    Router,
    SingleLayerRouter,
    TaskCodeRouter,
    TaskCodeSingleLayerRouter,
    TaskRouter,
    TaskSingleLayerRouter,
)

# 单层四个变体：(task, code, single) ⇒ 期望类。基线四类的对应项在
# ``_SINGLE_LAYER_FALSE_MATRIX`` 里（flag-off 必须拿到**同一对象**）。
_SINGLE_LAYER_TRUE_MATRIX = (
    (False, False, SingleLayerRouter),
    (True, False, TaskSingleLayerRouter),
    (False, True, CodeSingleLayerRouter),
    (True, True, TaskCodeSingleLayerRouter),
)

_SINGLE_LAYER_FALSE_MATRIX = (
    (False, False, Router),
    (True, False, TaskRouter),
    (False, True, CodeRouter),
    (True, True, TaskCodeRouter),
)


def _state(question: str) -> AgentState:
    return AgentState(messages=[HumanMessage(content=question)])


class _StubStructuredModel:
    """``get_llm_client`` 桩：记录 schema、回放固定输出。"""

    def __init__(self, output=None, *, error: Exception | None = None):
        self._output = output
        self._error = error
        self.calls = 0
        self.schemas: list = []

    def with_structured_output(self, cls):
        self.schemas.append(cls)
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._output


class _CountingKBLLM:
    """A3 的 llm 桩。

    ``with_structured_output`` 是在**节点工厂构造期**被调用的（``create_router_node``
    里就把链拼好了），它不是"这一次花没花钱"的观测点；真正花钱的是链上的调用。
    ``prompt | chain`` 会被 langchain 包成 ``RunnableLambda`` 以**同步**方式调用返回
    对象本身，所以链必须是"可调用"的 RunnableLambda，而不是只带 ``ainvoke`` 的对象
    （同 ``tests/test_kb_nodes.py::_make_llm`` 的实测结论）。
    """

    def __init__(self, decision: KBRouteDecision):
        self.calls: list[int] = []
        self._llm = MagicMock()
        self._llm.with_structured_output = MagicMock(return_value=RunnableLambda(self._one_call))
        self._decision = decision

    def _one_call(self, _input):
        self.calls.append(1)
        return self._decision

    def with_structured_output(self, cls):
        return self._llm.with_structured_output(cls)


@pytest.fixture(autouse=True)
def _clean_routing_flags(monkeypatch):
    """每例把路由相关 flag 显式关死（不依赖宿主机 .env 的缺省值）。

    宿主导出 ``TASK_EXEC_ENABLED`` / ``ROUTING_FASTPATH_ENABLED`` 等任一项都会
    让"flag-off 等价"用例假红或假绿，因此一律在此定死，由各用例自行翻开需要的闸。
    """
    monkeypatch.setattr(settings, "TASK_EXEC_ENABLED", False)
    monkeypatch.setattr(settings, "CODE_EXEC_ENABLED", False)
    monkeypatch.setattr(settings, "ROUTING_SINGLE_LAYER_ENABLED", False)
    monkeypatch.setattr(settings, "ROUTING_FASTPATH_ENABLED", False)
    monkeypatch.setattr(settings, "ROUTING_CACHE_ENABLED", False)
    monkeypatch.setattr(settings, "AGENTIC_RAG_ENABLED", False)
    monkeypatch.setattr(settings, "CLARIFY_ROUTING_ENABLED", False)
    monkeypatch.setenv(f"{ablation.ENV_PREFIX}ROUTER_ENABLED", "true")
    monkeypatch.setenv(f"{ablation.ENV_PREFIX}RETRIEVAL_ENABLED", "true")
    assert ablation.is_enabled("router_enabled") is True
    yield


# ===========================================================================
# 1) flag-off 逐位等价（纪律 1）
# ===========================================================================


class TestFlagOffEquivalence:
    def test_new_key_default_is_off(self):
        """业务总闸默认关（纪律 1）：钉**字段声明的默认值**，不吃宿主机 .env。"""
        assert type(settings).model_fields["ROUTING_SINGLE_LAYER_ENABLED"].default is False

    def test_exec_flags_is_three_booleans_all_off(self):
        flags = router_mod._exec_flags()
        assert flags == (False, False, False), "三处判定共用同一源，默认全关"

    @pytest.mark.parametrize("task_on,code_on,expected", _SINGLE_LAYER_FALSE_MATRIX)
    def test_router_class_flag_off_is_the_same_objects(self, task_on, code_on, expected):
        """flag-off ⇒ 返回值与现网**同一对象**（不是"结构相同"，是 is 同一类）。"""
        assert router_mod._router_class(task_on, code_on, False) is expected

    def test_prompt_text_flag_off_is_byte_equal_to_baseline(self):
        assert router_mod._router_prompt_text(False, False, False) == ROUTER_SYSTEM_PROMPT
        assert router_mod._router_prompt_text(False, False, False).encode(
            "utf-8"
        ) == ROUTER_SYSTEM_PROMPT.encode("utf-8")

    @pytest.mark.parametrize(
        "task_on,code_on",
        [(False, False), (True, False), (False, True), (True, True)],
    )
    def test_prompt_text_flag_off_matches_existing_concatenation(self, task_on, code_on):
        """既有拼接结果一字不变：单层闸关 ⇒ 新块**不进任何提示词**。"""
        expected = ROUTER_SYSTEM_PROMPT
        if task_on:
            expected += TASK_EXEC_ROUTE_BLOCK
        if code_on:
            expected += CODE_EXEC_ROUTE_BLOCK
        assert router_mod._router_prompt_text(task_on, code_on, False) == expected

    def test_single_layer_block_is_not_in_baseline_prompt(self):
        """反向断言：基座一字节未动，新块只活在段里。"""
        assert "kb_route" in SINGLE_LAYER_ROUTE_BLOCK
        assert "kb_route" not in ROUTER_SYSTEM_PROMPT

    @pytest.mark.asyncio
    async def test_flag_off_pipeline_uses_base_schema_and_emits_no_kb_params(self, monkeypatch):
        model = _StubStructuredModel(Router(type="kb-query", logic="llm: baseline"))
        monkeypatch.setattr(router_mod, "get_llm_client", lambda **kwargs: model)

        result = await router_mod.analyze_and_route_query(_state("什么是示例分类 A"), config={})

        assert model.schemas == [Router]
        assert type(result["router"]) is Router
        assert getattr(result["router"], "kb_route", None) is None
        assert getattr(result["router"], "kb_tools", None) in (None, [])


# ===========================================================================
# 2) 换代令牌：单层闸必须是令牌的一维
# ===========================================================================


class TestGenerationToken:
    def test_single_layer_flag_changes_token_and_reverting_restores_it(self, monkeypatch):
        base = routing_cache.generation_token()

        monkeypatch.setattr(settings, "ROUTING_SINGLE_LAYER_ENABLED", True)
        on = routing_cache.generation_token()
        assert on != base, "单层开关必须进换代令牌，否则单层结论会被三种请求命中"

        monkeypatch.setattr(settings, "ROUTING_SINGLE_LAYER_ENABLED", False)
        assert routing_cache.generation_token() == base, "翻回去必须与初始值逐字相同"

    def test_token_changes_across_the_four_single_layer_variants(self, monkeypatch):
        """单层开 + task/code 四种组合 ⇒ 令牌两两不同（类名维度仍参与）。"""
        monkeypatch.setattr(settings, "ROUTING_SINGLE_LAYER_ENABLED", True)
        tokens = set()
        for task_on, code_on in ((False, False), (True, False), (False, True), (True, True)):
            monkeypatch.setattr(settings, "TASK_EXEC_ENABLED", task_on)
            monkeypatch.setattr(settings, "CODE_EXEC_ENABLED", code_on)
            tokens.add(routing_cache.generation_token())
        assert len(tokens) == 4, "四种 schema 变体必须是四个不同的换代令牌"


# ===========================================================================
# 3) A1：单层变体 + 产参 + 不伪造
# ===========================================================================


class TestSingleLayerProducesParams:
    @pytest.mark.parametrize("task_on,code_on,expected", _SINGLE_LAYER_TRUE_MATRIX)
    def test_router_class_flag_on_maps_to_variant(self, task_on, code_on, expected):
        assert router_mod._router_class(task_on, code_on, True) is expected
        assert issubclass(expected, Router)

    @pytest.mark.parametrize("task_on,code_on,expected", _SINGLE_LAYER_TRUE_MATRIX)
    def test_variant_class_names_are_stable(self, task_on, code_on, expected):
        """``generation_token()`` 拿 ``__name__`` 当成分 ⇒ 类名必须可预期。"""
        cls = router_mod._router_class(task_on, code_on, True)
        assert cls.__name__ == expected.__name__
        assert cls.__name__.endswith("Router")

    def test_variant_widens_kb_fields_only(self):
        """基类字段一个不动：单层变体只加两个 KB 参数，``type`` 词汇继承自基类。"""
        assert (
            Router.model_fields["type"].annotation
            == SingleLayerRouter.model_fields["type"].annotation
        )
        assert set(SingleLayerRouter.model_fields) - set(Router.model_fields) == {
            "kb_route",
            "kb_tools",
        }
        assert SingleLayerRouter().kb_route is None
        assert SingleLayerRouter().kb_tools == []

    def test_flag_off_json_schema_is_baseline_six_properties(self):
        """flag-off 的结构化输出 schema 一字未扩（基类只有 6 个属性）。

        单层参数做成**子类**而不是往基类加字段，就是为了这条：基类 schema 一变，
        flag-off 的"逐位等价"就破了。
        """
        assert sorted(Router.model_json_schema()["properties"]) == [
            "confidence",
            "decision",
            "logic",
            "question",
            "reasoning",
            "type",
        ]
        variant_props = sorted(SingleLayerRouter.model_json_schema()["properties"])
        assert variant_props == sorted(
            [*Router.model_json_schema()["properties"], "kb_route", "kb_tools"]
        )

    def test_kb_channel_vocabulary_matches_a3_decision(self):
        """通道词汇与 A3 的 ``KBRouteDecision`` 同一套（不另立第三套通道语义）。"""
        variant = SingleLayerRouter.model_json_schema()["properties"]
        a3 = KBRouteDecision.model_json_schema()["properties"]
        # kb_route 是 Optional ⇒ anyOf[通道枚举, null]；枚举本体必须与 A3 的 route 同集合
        route_branch = next(branch for branch in variant["kb_route"]["anyOf"] if "enum" in branch)
        assert set(route_branch["enum"]) == set(a3["route"]["enum"])
        assert set(variant["kb_tools"]["items"]["enum"]) == set(a3["tools"]["items"]["enum"])

    def test_prompt_appends_block_at_the_end(self):
        assert (
            router_mod._router_prompt_text(False, False, True)
            == ROUTER_SYSTEM_PROMPT + SINGLE_LAYER_ROUTE_BLOCK
        )
        assert (
            router_mod._router_prompt_text(True, True, True)
            == ROUTER_SYSTEM_PROMPT
            + TASK_EXEC_ROUTE_BLOCK
            + CODE_EXEC_ROUTE_BLOCK
            + SINGLE_LAYER_ROUTE_BLOCK
        )

    @pytest.mark.asyncio
    async def test_flag_on_carries_kb_params_out_of_analyze(self, monkeypatch):
        """A1 产参必须**带出到返回值**（sanitized_router 构造处不得把它丢掉）。"""
        monkeypatch.setattr(settings, "ROUTING_SINGLE_LAYER_ENABLED", True)
        output = SingleLayerRouter(
            type="kb-query",
            logic="llm: single layer",
            question="示例数据集 A 的构建历程",
            decision="kb",
            confidence=0.71,
            kb_route="hybrid",
            kb_tools=["postgres"],
        )
        model = _StubStructuredModel(output)
        monkeypatch.setattr(router_mod, "get_llm_client", lambda **kwargs: model)

        result = await router_mod.analyze_and_route_query(
            _state("示例数据集 A 的构建历程"), config={}
        )

        assert model.schemas == [SingleLayerRouter]
        emitted = result["router"]
        assert emitted.type == "kb-query"
        assert emitted.kb_route == "hybrid"
        assert emitted.kb_tools == ["postgres"]
        # 基类字段照常带出（等价性：单层只是**多了**两个字段）
        assert emitted.logic == "llm: single layer"
        assert emitted.decision == "kb"
        assert emitted.confidence == 0.71

    @pytest.mark.asyncio
    async def test_dispatch_still_keyed_on_type_vocabulary_unchanged(self, monkeypatch):
        """route_type 词汇零改动：分派仍按 ``type``，单层参数只是附带产物。"""
        monkeypatch.setattr(settings, "ROUTING_SINGLE_LAYER_ENABLED", True)
        model = _StubStructuredModel(
            SingleLayerRouter(
                type="kb-query",
                logic="llm: single layer",
                question="什么是示例分类 A",
                kb_route="local",
                kb_tools=["postgres", "milvus"],
            )
        )
        monkeypatch.setattr(router_mod, "get_llm_client", lambda **kwargs: model)
        state = _state("什么是示例分类 A")

        result = await router_mod.analyze_and_route_query(state, config={})
        state.router = result["router"]

        assert router_mod.route_query(state) == "create_kb_query"
        assert result["router"].type in router_mod.ALLOWED_TYPES

    @pytest.mark.asyncio
    async def test_llm_exception_fallback_does_not_fabricate_kb_channel(self, monkeypatch):
        """兜底出口（异常）不产 kb 参数：那是有意的降级，A3 照常自己调模型。"""
        monkeypatch.setattr(settings, "ROUTING_SINGLE_LAYER_ENABLED", True)
        model = _StubStructuredModel(error=RuntimeError("router llm down"))
        monkeypatch.setattr(router_mod, "get_llm_client", lambda **kwargs: model)

        result = await router_mod.analyze_and_route_query(_state("什么是示例分类 A"), config={})

        assert result["router"].type == "kb-query"
        assert getattr(result["router"], "kb_route", None) is None

    @pytest.mark.asyncio
    async def test_fastpath_hit_does_not_fabricate_kb_channel(self, monkeypatch):
        """fast-path 命中同样不产参数（它压根没问模型，通道无从谈起）。"""
        monkeypatch.setattr(settings, "ROUTING_SINGLE_LAYER_ENABLED", True)
        monkeypatch.setattr(settings, "ROUTING_FASTPATH_ENABLED", True)
        monkeypatch.setattr(router_mod, "get_llm_client", _boom_llm)

        result = await router_mod.analyze_and_route_query(_state("你好"), config={})

        assert result["router"].type == "general-query"
        assert getattr(result["router"], "kb_route", None) is None

    @pytest.mark.asyncio
    async def test_cache_hit_does_not_carry_kb_params(self, monkeypatch):
        """能力缺口（本批刻意不扩缓存字段）：缓存命中的请求拿不到单层参数。"""
        monkeypatch.setattr(settings, "ROUTING_SINGLE_LAYER_ENABLED", True)
        monkeypatch.setattr(settings, "ROUTING_CACHE_ENABLED", True)
        first = _StubStructuredModel(
            SingleLayerRouter(
                type="kb-query",
                logic="llm: single layer",
                question="什么是示例分类 A",
                kb_route="hybrid",
                kb_tools=["milvus"],
            )
        )
        monkeypatch.setattr(router_mod, "get_llm_client", lambda **kwargs: first)
        question = "什么是示例分类 A"
        await router_mod.analyze_and_route_query(_state(question), config={})

        monkeypatch.setattr(router_mod, "get_llm_client", _boom_llm)
        hit = await router_mod.analyze_and_route_query(_state(question), config={})

        assert hit["router"].type == "kb-query"
        assert (
            getattr(hit["router"], "kb_route", None) is None
        ), "缓存值仍是四个基线字段：单层参数不进缓存（A3 此时自调模型，跳数=基线）"


def _boom_llm(**_kwargs):
    raise AssertionError("这条路径不该构造 LLM 客户端")


# ===========================================================================
# 4) A3：消费 L1 产物 ⇒ 0 次模型调用，且钳制照旧
# ===========================================================================


class TestRouterNodeShortCircuit:
    @pytest.mark.asyncio
    async def test_forwarded_external_is_still_clamped_to_local(self):
        """转发的通道**必须过安全钳制**：漏掉就等于让 L1 越过 KB_ENABLE_EXTERNAL_SEARCH。"""
        llm = _CountingKBLLM(KBRouteDecision(route="local", rationale="不该被用", tools=[]))
        node = create_router_node(llm, allow_external_search=False)

        result = await node(
            {"question": "q", "history": [], "route": "external", "kb_tools": ["milvus"]}
        )

        assert llm.calls == [], "L1 已给参数 ⇒ 本节点不得再问模型"
        assert result["route"] == "local"
        # 钳制后 route=local 且 tools 非空 ⇒ 保留 L1 给的 milvus（不塞默认组合）
        assert result["kb_tools"] == ["milvus"]
        assert result["steps"] == ["router"]

    @pytest.mark.asyncio
    async def test_forwarded_hybrid_clamped_and_empty_tools_defaulted(self):
        llm = _CountingKBLLM(
            KBRouteDecision(route="local", rationale="不该被用", tools=["postgres"])
        )
        node = create_router_node(llm, allow_external_search=False)

        result = await node({"question": "q", "history": [], "route": "hybrid"})

        assert llm.calls == []
        assert result["route"] == "local"
        # 第二段：route != external 且 tools 空 ⇒ 默认 postgres + milvus 兜底
        assert result["kb_tools"] == ["postgres", "milvus"]

    @pytest.mark.asyncio
    async def test_forwarded_external_kept_when_allowed(self):
        llm = _CountingKBLLM(KBRouteDecision(route="local", rationale="不该被用", tools=[]))
        node = create_router_node(llm, allow_external_search=True)

        result = await node({"question": "q", "history": [], "route": "external", "kb_tools": []})

        assert llm.calls == []
        assert result["route"] == "external"
        # 与基线同语义：route == external 时不注入本地默认组合
        assert result["kb_tools"] == []

    @pytest.mark.asyncio
    async def test_forwarded_tools_filtered_to_known_sources(self):
        """第三段：工具过滤到 {milvus, postgres}——转发的值同样要过一遍。"""
        llm = _CountingKBLLM(KBRouteDecision(route="local", rationale="不该被用", tools=[]))
        node = create_router_node(llm, allow_external_search=True)

        result = await node(
            {"question": "q", "history": [], "route": "local", "kb_tools": ["redis", "milvus"]}
        )

        assert llm.calls == []
        assert result["kb_tools"] == ["milvus"]

    @pytest.mark.asyncio
    async def test_no_forwarded_route_calls_model_once(self):
        """没有 L1 参数 ⇒ 照常自己决策（降级路径，跳数回到基线）。"""
        llm = _CountingKBLLM(KBRouteDecision(route="local", rationale="r", tools=["postgres"]))
        node = create_router_node(llm, allow_external_search=False)

        result = await node({"question": "q", "history": []})

        assert len(llm.calls) == 1
        assert result["route"] == "local"
        assert result["kb_tools"] == ["postgres"]


# ===========================================================================
# 5) 输入面守卫：转发的键必须真能到达子图节点
# ===========================================================================


class TestKbInputStateCarriesForwardedParams:
    """``StateGraph(..., input=KBInputState)`` 会**按输入 schema 过滤**入参。

    这是本批最容易静默失效的地方：如果 ``KBInputState`` 里没有 ``route``/
    ``kb_tools`` 两个键，``create_kb_query`` 转发的参数会在框架层被丢掉，
    A3 依旧调模型（"省一跳"变成一句空话），而单元测试（第 4 组直接喂 dict）
    全是绿的。因此这里用**真实的两个 schema** 起一张单节点图钉住可达性。
    """

    def test_input_schema_declares_both_channels(self):
        annotations = KBInputState.__annotations__
        assert "route" in annotations, "转发的 route 会被 input schema 丢掉"
        assert "kb_tools" in annotations, "转发的 kb_tools 会被 input schema 丢掉"

    def test_forwarded_keys_reach_the_node(self):
        seen: dict = {}

        def probe(state: KBWorkflowState) -> dict:
            seen["route"] = state.get("route")
            seen["kb_tools"] = state.get("kb_tools")
            return {}

        builder = StateGraph(KBWorkflowState, input=KBInputState)
        builder.add_node("probe", probe)
        builder.add_edge(START, "probe")
        builder.add_edge("probe", END)
        graph = builder.compile()

        graph.invoke({"question": "q", "history": [], "route": "hybrid", "kb_tools": ["postgres"]})
        assert seen == {"route": "hybrid", "kb_tools": ["postgres"]}

        seen.clear()
        graph.invoke({"question": "q", "history": []})
        assert seen == {"route": None, "kb_tools": None}, "不转发时两个通道必须为空"


# ===========================================================================
# 6) create_kb_query 的转发判据：有参数才加键
# ===========================================================================


class _FakeKBWorkflow:
    def __init__(self, payload=None):
        self._payload = payload or {"answer": "答复", "sources": ["来源A"]}
        self.inputs: list = []

    async def ainvoke(self, inputs, *, config=None, **_kwargs):
        self.inputs.append(inputs)
        return dict(self._payload)


class _FakeKnowledgeGateway:
    def __init__(self, *_args, **_kwargs):
        pass


@pytest.fixture
def _kb_workflow_probe(monkeypatch):
    """把 create_kb_query 的外部依赖全桩掉，只留"入参 dict 长什么样"可观测。"""
    workflow = _FakeKBWorkflow()
    monkeypatch.setattr(general_handler_mod, "get_llm_client", lambda **kwargs: object())
    monkeypatch.setattr(general_handler_mod, "KnowledgeGateway", _FakeKnowledgeGateway)
    monkeypatch.setattr(multi_tool_mod, "create_kb_multi_tool_workflow", lambda **kwargs: workflow)
    return workflow


@pytest.mark.asyncio
async def test_create_kb_query_forwards_only_when_l1_gave_params(monkeypatch, _kb_workflow_probe):
    state = AgentState(
        messages=[HumanMessage(content="什么是示例分类 A")],
        router=SingleLayerRouter(
            type="kb-query",
            logic="llm",
            question="什么是示例分类 A",
            kb_route="local",
            kb_tools=["postgres", "milvus"],
        ),
    )

    await general_handler_mod.create_kb_query(state, config={})

    payload = _kb_workflow_probe.inputs[0]
    assert payload["route"] == "local"
    assert payload["kb_tools"] == ["postgres", "milvus"]


@pytest.mark.asyncio
async def test_create_kb_query_adds_no_keys_when_router_has_no_params(
    monkeypatch, _kb_workflow_probe
):
    """降级不伪造：**断言键不存在**（不是断言值为 None）——键存在但为空值会让
    A3 把"空通道"当成"L1 已决策"，跳过模型却拿不到任何通道。"""
    monkeypatch.setattr(settings, "ROUTING_SINGLE_LAYER_ENABLED", True)
    state = AgentState(
        messages=[HumanMessage(content="什么是示例分类 A")],
        router=Router(type="kb-query", logic="fallback", question="什么是示例分类 A"),
    )

    await general_handler_mod.create_kb_query(state, config={})

    payload = _kb_workflow_probe.inputs[0]
    assert "route" not in payload
    assert "kb_tools" not in payload
    assert set(payload) == {"question", "history"}


@pytest.mark.asyncio
async def test_create_kb_query_adds_no_keys_when_single_layer_declared_null_params(
    monkeypatch, _kb_workflow_probe
):
    """G-4 直接用例：不是 ``test_create_kb_query_adds_no_keys_when_router_has_no_params``
    的重复——那条走「基类没声明 ``kb_route`` ⇒ 外层条件假」，本条走「单层变体声明了
    但值为 ``None`` ⇒ 外层真、内层假」；生产判据是两层，缺口在第二层。
    钉住的回归形态：``general_handler`` 内层 ``kb_route_param is not None`` 判据被去掉
    （无条件加键）时本条必须红——空通道会让 A3 把"没决策"当成"已决策"，危害见上一条
    用例 docstring，此处不重抄。"""
    monkeypatch.setattr(settings, "ROUTING_SINGLE_LAYER_ENABLED", True)
    state = AgentState(
        messages=[HumanMessage(content="什么是示例分类 A")],
        router=SingleLayerRouter(
            type="kb-query",
            logic="llm",
            question="什么是示例分类 A",
            kb_route=None,
            kb_tools=[],
        ),
    )

    await general_handler_mod.create_kb_query(state, config={})

    payload = _kb_workflow_probe.inputs[0]
    assert "route" not in payload
    assert "kb_tools" not in payload
    assert set(payload) == {"question", "history"}


# ===========================================================================
# 7) 已知能力缺口：缓存字段不扩（本批刻意不动 T8 语义）
# ===========================================================================


class TestKnownCapabilityGap:
    def test_cached_fields_still_four_baseline_fields(self):
        assert routing_cache._CACHED_FIELDS == ("type", "logic", "decision", "confidence")
        assert "kb_route" not in routing_cache._CACHED_FIELDS
        assert "kb_tools" not in routing_cache._CACHED_FIELDS
