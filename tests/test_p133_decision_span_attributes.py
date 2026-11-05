"""PhaseK 任务 3：spans.py 决策属性 helper + 任务 4/5/6 消费点接线守卫。

覆盖面与验收口径（PhaseK 计划 §任务 3 + §审查重点第 47 行）：
- 三个 helper 写入的属性键名**逐字**钉死：``agentix.route.decision`` /
  ``gen_ai.response.model`` / ``agentix.resume_from_checkpoint``；
- 空串 / 无 span / span 或取 span 接缝抛异常 ⇒ 静默降级（fail-open），
  helper **绝不向上抛**——不得让一次正常问答因观测设施失败而失败；
- ``record_model_used`` 即使 model_used 与 primary 相同也写（语义是"实际服务方"）；
- ``record_resume_from_checkpoint`` 仅 thread_id_present 为真时写 ``True``，为假不写；
- helper 不新增任何指标/label（约定 3：判定值只进 span，不进指标）；
- 键名常量只有一个源（约定 6 ④）：``gen_ai.response.model`` 字面量只许出现在
  tracing.py 常量表，spans.py 必须经常量引用。

追加（PhaseK 任务 4/5/6，消费点接线）：
- 任务 4：``analyze_and_route_query`` 成功路径的落点（与缓存命中路径同走 ``record_route_decision`` 单源 helper）写最终 route，值 ∈ 7 类词表；
- 任务 5：``query_knowledge_base`` 两处 ``gw_result`` 分支写实际服务方，
  主模型名同源 ``ctx.model_name``；
- 任务 6：进环 ``resume_in_loop`` 与断点 ``resume_thread`` 两条恢复路径按
  ``config["configurable"]["thread_id"]`` 非空写恢复属性，空则属性缺席。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agentix.config import settings
from agentix.observability import spans as spans_mod
from agentix.observability.spans import (
    RESUME_ATTR,
    ROUTE_DECISION_ATTR,
    record_model_used,
    record_resume_from_checkpoint,
    record_route_decision,
)
from agentix.observability.tracing import GENAI_RESPONSE_MODEL


class FakeSpan:
    """最小 span 替身：记录 set_attribute 载荷；explode=True 时写入即抛。"""

    def __init__(self, *, explode: bool = False):
        self.attrs: dict = {}
        self._explode = explode

    def set_attribute(self, key, value):
        if self._explode:
            raise RuntimeError("span broken")
        self.attrs[key] = value


@pytest.fixture()
def fake_span(monkeypatch):
    """把 spans 的取当前 span 接缝换成可记属性的假对象。"""
    span = FakeSpan()
    monkeypatch.setattr(spans_mod, "_current_span", lambda: span)
    return span


# ---------------------------------------------------------------------------
# 属性写入（键名逐字 + 有界判定值）
# ---------------------------------------------------------------------------


def test_record_route_decision_writes_attr_on_fake_span(fake_span):
    record_route_decision("rag-query")
    assert fake_span.attrs[ROUTE_DECISION_ATTR] == "rag-query"
    # 键名逐字钉死（消费点与文档引用以此为准）
    assert ROUTE_DECISION_ATTR == "agentix.route.decision"


def test_record_route_decision_empty_string_is_noop(fake_span):
    record_route_decision("")
    assert fake_span.attrs == {}


def test_record_model_used_writes_genai_response_model(fake_span):
    record_model_used("deepseek-fallback-1", primary_model="gpt-4o")
    assert fake_span.attrs[GENAI_RESPONSE_MODEL] == "deepseek-fallback-1"
    # 键名逐字 == OTel GenAI semconv（常量真源在 tracing.py）
    assert GENAI_RESPONSE_MODEL == "gen_ai.response.model"


def test_record_model_used_writes_even_when_same_as_primary(fake_span):
    # 语义是"实际服务方"，不是"是否发生了回退"：相同也必须写
    record_model_used("gpt-4o", primary_model="gpt-4o")
    assert fake_span.attrs[GENAI_RESPONSE_MODEL] == "gpt-4o"


def test_record_model_used_empty_string_is_noop(fake_span):
    record_model_used("", primary_model="gpt-4o")
    assert fake_span.attrs == {}


def test_record_resume_from_checkpoint_only_when_thread_present(monkeypatch):
    span = FakeSpan()
    monkeypatch.setattr(spans_mod, "_current_span", lambda: span)
    record_resume_from_checkpoint(thread_id_present=True)
    assert span.attrs[RESUME_ATTR] is True
    assert RESUME_ATTR == "agentix.resume_from_checkpoint"
    # False ⇒ 不写该属性（任务 6 口径：无 thread_id 时属性缺席，而非写 False）
    record_resume_from_checkpoint(thread_id_present=False)
    assert span.attrs == {RESUME_ATTR: True}


# ---------------------------------------------------------------------------
# fail-open：无 span / 坏 span / 接缝抛异常，helper 都不许抛
# ---------------------------------------------------------------------------


def test_helpers_never_raise_when_span_is_noop_or_broken(monkeypatch):
    def _call_all():
        record_route_decision("rag-query")
        record_model_used("m", primary_model="p")
        record_resume_from_checkpoint(thread_id_present=True)

    # ① 当前无 span（_current_span 返回 None）
    monkeypatch.setattr(spans_mod, "_current_span", lambda: None)
    _call_all()  # 抛即红

    # ② 有 span 但 set_attribute 抛（OTel 侧异常）
    monkeypatch.setattr(spans_mod, "_current_span", lambda: FakeSpan(explode=True))
    _call_all()

    # ③ 取 span 的接缝本身就抛（最坏情况也要兜住）
    def _boom():
        raise RuntimeError("tracer boom")

    monkeypatch.setattr(spans_mod, "_current_span", _boom)
    _call_all()


def test_helpers_noop_when_no_span_at_all_without_monkeypatch():
    """不 monkeypatch：OTel 未装或未在 span 上下文内时走真实降级路径，仍不得抛。"""
    record_route_decision("general-query")
    record_model_used("m", primary_model="p")
    record_resume_from_checkpoint(thread_id_present=True)


# ---------------------------------------------------------------------------
# 纪律守卫：不新增指标；键名字面量单源
# ---------------------------------------------------------------------------


def test_no_new_metric_labels_registered(fake_span):
    """三个 helper 只写 span 属性，不触碰 Prometheus 注册表（约定 3）。"""
    from prometheus_client import REGISTRY

    # 指标名集合前后一致 ⇒ 没有新指标被注册（label 挂在指标上，无新指标即无新 label 面）
    before = set(REGISTRY._names_to_collectors)
    record_route_decision("rag-query")
    record_model_used("m", primary_model="p")
    record_resume_from_checkpoint(thread_id_present=True)
    assert set(REGISTRY._names_to_collectors) == before


def test_response_model_literal_single_source():
    """``gen_ai.response.model`` 字面量只在 tracing.py 出现，spans.py 经常量引用（约定 6 ④）。"""
    root = Path(__file__).resolve().parents[1]
    spans_src = (root / "agentix" / "observability" / "spans.py").read_text(encoding="utf-8")
    assert '"gen_ai.response.model"' not in spans_src
    tracing_src = (root / "agentix" / "observability" / "tracing.py").read_text(encoding="utf-8")
    assert 'GENAI_RESPONSE_MODEL = "gen_ai.response.model"' in tracing_src


# ---------------------------------------------------------------------------
# 任务 4 消费点：路由决策值上 span（analyze_and_route_query 成功路径的落点，与缓存命中路径同走 record_route_decision 单源）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_records_route_decision_attr(fake_span, monkeypatch):
    """成功路径拿到最终 route 后必须写 ``agentix.route.decision``（PhaseK 任务 4）。

    假模型桩沿用 p118 既有范式（``get_llm_client`` → ``with_structured_output``
    → 固定结果），不新造桩形态；flag 显式钉到基线组合（fastpath/task/code/cache 全关），
    保证走的就是"白名单校验通过"的成功路径落点（与缓存命中路径同走 ``record_route_decision`` 单源 helper），且词表就是 7 类。
    """
    from langchain_core.messages import HumanMessage

    from agentix.application.agents import router as router_module
    from agentix.config import ablation
    from agentix.orchestration.langgraph.lg_states import AgentState, Router

    for _name in tuple(ablation.FLAG_DEFAULTS):
        monkeypatch.delenv(f"{ablation.ENV_PREFIX}{_name.upper()}", raising=False)
    monkeypatch.setattr(router_module.settings, "ROUTING_FASTPATH_ENABLED", False, raising=False)
    monkeypatch.setattr(router_module.settings, "TASK_EXEC_ENABLED", False, raising=False)
    monkeypatch.setattr(router_module.settings, "CODE_EXEC_ENABLED", False, raising=False)
    # 再 pin 一枚缓存开关到基线（关），与上面三行逐字同形：宿主 .env 若开了
    # ROUTING_CACHE_ENABLED，本用例就不再稳定走"成功路径"这一支（缓存命中会跳过
    # 这次 LLM 调用），读数随环境漂移，钉的是读数不随宿主 .env 的缓存开关漂移——缓存显式关才与用例 docstring
    # 声明的基线组合一致，钉住的是成功路径那个落点。
    monkeypatch.setattr(router_module.settings, "ROUTING_CACHE_ENABLED", False, raising=False)

    class _Structured:
        def __init__(self, result):
            self._result = result

        async def ainvoke(self, messages):
            return self._result

    class _Model:
        def with_structured_output(self, schema, **kwargs):
            return _Structured(Router(type="general-query", logic="l", question="q"))

    monkeypatch.setattr(router_module, "get_llm_client", lambda **kw: _Model())

    result = await router_module.analyze_and_route_query(
        AgentState(messages=[HumanMessage(content="你好")]), config={}
    )

    route_value = fake_span.attrs[ROUTE_DECISION_ATTR]
    # 属性值必须与返回的最终 route 一致（判定单源，不许 span 与实际各写一套）
    assert route_value == result["router"].type
    # 有界词表守卫：值 ∈ router.py 的 7 类 ALLOWED_TYPES（约定 3：只落可枚举判定值）
    assert route_value in router_module.ALLOWED_TYPES
    assert len(router_module.ALLOWED_TYPES) == 7


# ---------------------------------------------------------------------------
# 任务 5 消费点：回退后实际服务模型上 span（chat_agent_tools 两处 gw_result 分支）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_result_records_actual_model_on_span(fake_span, monkeypatch):
    """两处 ``gw_result`` 分支都要把 ``model_used`` 写上 span（PhaseK 任务 5）。

    桩范式取自 test_r3_model_gateway / chat_service 既有形态：gateway 整体替身，
    只钉"消费点取的是 GatewayResult.model_used、主模型名取的是 ctx.model_name"。
    ``model_used != ctx.model_name`` 由桩构造（fallback-m vs primary-m）。
    """
    from types import SimpleNamespace

    import agentix.infrastructure.llm.model_gateway as model_gateway_mod
    import agentix.retrieval.domain.services.context_engine as context_engine
    import agentix.retrieval.usecases.chat_agent_tools as cat
    import agentix.retrieval.usecases.chat_service as chat_service
    from agentix.observability.tracing import GENAI_RESPONSE_MODEL

    # 检索/装配/提示词装配整段替身：本用例只验证 gw_result 之后的观测消费点
    # （chat_scope 三函数在 chat_agent_tools 是模块级导入 ⇒ 就地替身；
    #   chat_service 各函数在工具体内延迟导入 ⇒ 替身打在源模块上）
    monkeypatch.setattr(cat.settings, "RERANKER_ENABLED", False, raising=False)
    monkeypatch.setattr(cat, "retrieval_filter_for_question", lambda session, **kw: ({}, None))
    monkeypatch.setattr(cat, "document_title_for_id", lambda session, doc_id: None)

    async def _retrieve(**kw):
        return [SimpleNamespace(page_content="c1")]

    monkeypatch.setattr(chat_service, "retrieve_scoped_docs_cached", _retrieve)
    monkeypatch.setattr(chat_service, "get_citations_from_docs", lambda docs, session: [])
    monkeypatch.setattr(chat_service, "hit_chunks_from_docs", lambda docs: [])
    monkeypatch.setattr(chat_service, "build_prompt", lambda *a, **k: "PROMPT")
    monkeypatch.setattr(
        context_engine, "assemble_context", lambda docs, **kw: ("CTX", {"final_docs": 1})
    )

    gw_result = model_gateway_mod.GatewayResult(
        response=SimpleNamespace(content="回答文本"),
        model_used="fallback-m",
        attempts=2,
    )
    primary_seen: list = []

    class _Gateway:
        specs: list = []
        chain: list = []

        def fallback_specs(self, *, primary_model):
            return self.specs

        def fallback_chain(self, *, primary_model):
            return self.chain

        async def ainvoke_with_fallback(self, prompt, *, primary_model, fallback_models, build_client, invoke):
            # 记录主模型名来源：必须是调用点透传的 ctx.model_name（判定单源，约定 6 ④）
            primary_seen.append(primary_model)
            return gw_result

    monkeypatch.setattr(model_gateway_mod, "get_model_gateway", lambda: _Gateway())

    ctx = cat.AgentToolContext(
        session=None,
        kb_id="kb",
        scope_type="kb",
        scope_id=None,
        vectorstore=None,
        metadata_filter=None,
        search_k=4,
        cache_redis=None,
        artifact_id="a",
        scope_token="s",
        model_name="primary-m",
    )
    kb_tool = next(t for t in cat.build_chat_agent_tools(ctx) if t.name == "query_knowledge_base")

    # 分支①：跨端点 specs 非空（:285/:294 处）
    _Gateway.specs = [SimpleNamespace(model="fb-m", base_url=None, api_key=None)]
    _Gateway.chain = []
    out = await kb_tool.ainvoke({"question": "问题"})
    assert out == "回答文本"
    assert fake_span.attrs[GENAI_RESPONSE_MODEL] == "fallback-m"
    assert fake_span.attrs[GENAI_RESPONSE_MODEL] != ctx.model_name

    # 分支②：specs 空、fallback_chain 非空（:303/:312 处）
    fake_span.attrs.clear()
    _Gateway.specs = []
    _Gateway.chain = ["m2"]
    await kb_tool.ainvoke({"question": "问题"})
    assert fake_span.attrs[GENAI_RESPONSE_MODEL] == "fallback-m"

    # 两处调用点的主模型名都取自 ctx.model_name（不许另经 settings/resolve 取源）
    assert primary_seen == ["primary-m", "primary-m"]


# ---------------------------------------------------------------------------
# 任务 6 消费点：恢复点上 span（进环 resume 与断点续跑，两条路径）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_paths_mark_checkpoint_attr(fake_span, monkeypatch):
    """两条恢复路径都要在 config 的 thread_id 非空时写 ``agentix.resume_from_checkpoint``，
    无 thread_id 时**不写该属性**（PhaseK 任务 6；判据只许来自
    ``config["configurable"]["thread_id"]``，不得另取 thread 概念——约定 6 ④）。"""
    import json

    # ---- 路径①：lg_builder.resume_in_loop（进环 Command(resume=...)）----
    from agentix.orchestration.langgraph import lg_builder

    cfg_seen_in_loop: list = []

    class _GraphStub:
        async def ainvoke(self, input, config=None):
            cfg_seen_in_loop.append(config)
            return {}

    monkeypatch.setattr(lg_builder, "graph", _GraphStub())
    await lg_builder.resume_in_loop("t-1", "approve")
    # LG-2-3：config 形状统一由 graph_config.thread_config 产出——configurable 里
    # 只有本路径的 thread_id，顶层多一个 recursion_limit（同主链路的轮数预算）。
    assert cfg_seen_in_loop[-1]["configurable"] == {"thread_id": "t-1"}
    assert cfg_seen_in_loop[-1]["recursion_limit"] == settings.MAX_ITERATIONS
    assert fake_span.attrs[RESUME_ATTR] is True

    # 无 thread_id ⇒ 属性缺席（不是写 False）
    fake_span.attrs.clear()
    await lg_builder.resume_in_loop("", "approve")
    assert RESUME_ATTR not in fake_span.attrs

    # ---- 路径②：breakpoint_resume.CheckpointRecovery.resume_thread（断点续跑）----
    from agentix.application.services.breakpoint_resume import CheckpointRecovery

    class _FakeRedis:
        async def get(self, key):
            return json.dumps({"thread_id": "t-2", "last_node": "execute_task"})

        async def set(self, key, value, ex=None):
            return None

        async def srem(self, key, *members):
            return None

    cfg_seen_recovery: list = []

    class _RecoveryGraphStub:
        def get_state(self, config):
            return type("S", (), {"values": {"messages": []}})()

        async def ainvoke(self, input, config=None):
            cfg_seen_recovery.append(config)
            return {"messages": []}

    recovery = CheckpointRecovery(redis_client=_FakeRedis())
    await recovery.resume_thread("t-2", _RecoveryGraphStub())
    assert cfg_seen_recovery[-1]["configurable"]["thread_id"] == "t-2"
    assert fake_span.attrs[RESUME_ATTR] is True

    fake_span.attrs.clear()
    await recovery.resume_thread("", _RecoveryGraphStub())
    assert RESUME_ATTR not in fake_span.attrs


# ---------------------------------------------------------------------------
# 路由决策属性落点层级统一
#
# B2-1 是结构守卫：fake_span 是单枚替身、不模拟嵌套，任何行为用例都分不清
# "属性挂在哪一层 span 上"——层级只能钉在 AST 结构上（同 test_p125 /
# test_p127 的 AST 守卫风格，不自造框架）。
# ---------------------------------------------------------------------------

ROUTER_PY = Path(__file__).resolve().parents[1] / "agentix" / "application" / "agents" / "router.py"


def _lookup_cache_fn() -> ast.AsyncFunctionDef:
    tree = ast.parse(ROUTER_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_lookup_routing_cache":
            return node
    raise AssertionError("router.py 里找不到 async def _lookup_routing_cache()")


def _is_direct_call(node: ast.AST, name: str) -> bool:
    """节点是否为 ``name(...)``（裸名）或 ``<mod>.name(...)``（属性）调用位。

    两种形态都认：判据不许因无害的 import 形态变化误伤（对齐 p127 ``_refs`` 的理由）。
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == name) or (
        isinstance(func, ast.Attribute) and func.attr == name
    )


def _cache_hit_async_with(fn: ast.AsyncFunctionDef):
    """在函数体内找 ``async with span("route.cache_hit", ...)`` 的 AsyncWith 节点。"""
    for node in ast.walk(fn):
        if not isinstance(node, ast.AsyncWith):
            continue
        for item in node.items:
            ctx = item.context_expr
            if (
                _is_direct_call(ctx, "span")
                and ctx.args
                and isinstance(ctx.args[0], ast.Constant)
                and ctx.args[0].value == "route.cache_hit"
            ):
                return node
    return None


def test_b2_1_route_decision_recorded_outside_cache_hit_span():
    """命中腿的 ``record_route_decision`` 必须在 ``route.cache_hit`` 子 span **之外**
    （函数体顶层、源码顺序先于该 span）——G2 收口：两条腿同落"当前环境 span"层级。"""
    fn = _lookup_cache_fn()
    span_block = _cache_hit_async_with(fn)
    async_with_linenos = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.AsyncWith)]
    assert span_block is not None, (
        f"_lookup_routing_cache（def lineno={fn.lineno}）体内找不到 "
        f'async with span("route.cache_hit", ...)；实际 AsyncWith lineno={async_with_linenos}'
    )

    # ② span 体内不得含 record_route_decision 调用（改前形态记点在体内 ⇒ 必红）
    inside_calls = [
        n for n in ast.walk(span_block) if _is_direct_call(n, "record_route_decision")
    ]
    assert not inside_calls, (
        f"route.cache_hit 的 AsyncWith（lineno={span_block.lineno}）体内仍出现 "
        f"record_route_decision（lineno={[n.lineno for n in inside_calls]}）——"
        "决策属性被记进了子 span 层级，G2 未收口"
    )

    # ③ 函数体顶层（不在任何 with 体内）恰好一枚 record_route_decision，且先于该 span
    within_bodies: set = set()
    for node in ast.walk(fn):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for stmt in node.body:
                for sub in ast.walk(stmt):
                    within_bodies.add(id(sub))
    all_calls = [n for n in ast.walk(fn) if _is_direct_call(n, "record_route_decision")]
    top_calls = [n for n in all_calls if id(n) not in within_bodies]
    assert len(top_calls) == 1, (
        f"_lookup_routing_cache 顶层（非 with 体内）record_route_decision 应恰好一枚，"
        f"实际顶层={[(n.lineno, n.col_offset) for n in top_calls]}，"
        f"全部调用={[(n.lineno, n.col_offset) for n in all_calls]}，"
        f"span_block lineno={span_block.lineno}"
    )
    assert top_calls[0].lineno < span_block.lineno, (
        f"顶层 record_route_decision（lineno={top_calls[0].lineno}）必须**先于** "
        f"route.cache_hit 的 AsyncWith（lineno={span_block.lineno}）"
    )


@pytest.mark.asyncio
async def test_b2_2_both_route_legs_record_decision_exactly_once(fake_span, monkeypatch):
    """缓存命中腿与 LLM 成功腿各**恰好一次**写 ``agentix.route.decision``，
    且值与返回的 ``router.type`` 同源（行为对照）。

    **鉴别力边界（诚实标注）**：``fake_span`` 接缝是单枚 FakeSpan，**不模拟嵌套**
    ⇒ 本用例分不清"属性挂在哪一层 span 上"——层级统一由 B2-1
    （``test_b2_1_route_decision_recorded_outside_cache_hit_span``）的结构守卫钉；
    本用例只钉"每请求恰好记一枚、值与返回同源、无重复写、无静默不写"。
    """
    from langchain_core.messages import HumanMessage

    from agentix.application.agents import router as router_module
    from agentix.application.agents import routing_cache
    from agentix.config import ablation
    from agentix.orchestration.langgraph.lg_states import AgentState, Router

    for _name in tuple(ablation.FLAG_DEFAULTS):
        monkeypatch.delenv(f"{ablation.ENV_PREFIX}{_name.upper()}", raising=False)
    monkeypatch.setattr(router_module.settings, "ROUTING_FASTPATH_ENABLED", False, raising=False)
    monkeypatch.setattr(router_module.settings, "TASK_EXEC_ENABLED", False, raising=False)
    monkeypatch.setattr(router_module.settings, "CODE_EXEC_ENABLED", False, raising=False)

    class _Structured:
        def __init__(self, result):
            self._result = result

        async def ainvoke(self, messages):
            return self._result

    class _Model:
        def with_structured_output(self, schema, **kwargs):
            return _Structured(Router(type="general-query", logic="l", question="q"))

    # 模型工厂计数替身：腿①（缓存命中）断言零调用——没有这枚计数，"重建失败降级走
    # LLM"也会凑出恰好一次写点，腿①就白钉了。
    model_calls: list = []

    def _counting_get_llm_client(**kw):
        model_calls.append(kw)
        return _Model()

    monkeypatch.setattr(router_module, "get_llm_client", _counting_get_llm_client)

    calls: list = []
    real_record = router_module.record_route_decision

    def _spy(value):
        calls.append(value)
        return real_record(value)

    monkeypatch.setattr(router_module, "record_route_decision", _spy)

    class _StubCache:
        def get(self, key, *, generation=None):
            return {
                "type": "general-query",
                "logic": "l",
                "decision": "d",
                "confidence": 0.9,
            }

    # ---- 腿①：缓存命中（桩缓存必中）----
    monkeypatch.setattr(routing_cache, "generation_token", lambda: "g-beta2")
    monkeypatch.setattr(routing_cache, "get_routing_cache", lambda: _StubCache())
    monkeypatch.setattr(router_module.settings, "ROUTING_CACHE_ENABLED", True, raising=False)

    hit_result = await router_module.analyze_and_route_query(
        AgentState(messages=[HumanMessage(content="你好")]), config={}
    )
    assert model_calls == [], f"缓存命中腿不应构造模型，实际 model_calls={model_calls}"
    assert calls == [hit_result["router"].type], (
        f"缓存命中腿应恰好写一次决策属性，实际 calls={calls}"
    )
    assert fake_span.attrs[ROUTE_DECISION_ATTR] == hit_result["router"].type

    # ---- 腿②：LLM 成功（缓存显式关，基线成功路径）----
    calls.clear()
    model_calls.clear()
    fake_span.attrs.clear()
    monkeypatch.setattr(router_module.settings, "ROUTING_CACHE_ENABLED", False, raising=False)

    llm_result = await router_module.analyze_and_route_query(
        AgentState(messages=[HumanMessage(content="你好")]), config={}
    )
    assert len(model_calls) == 1, f"LLM 腿应恰好构造一次模型，实际 model_calls={model_calls}"
    assert calls == [llm_result["router"].type], (
        f"LLM 成功腿应恰好写一次决策属性，实际 calls={calls}"
    )
    assert fake_span.attrs[ROUTE_DECISION_ATTR] == llm_result["router"].type
