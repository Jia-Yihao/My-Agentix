"""T30-3：单层路由（``ROUTING_SINGLE_LAYER_ENABLED``）**跳数差值**取证（独立入口）。

规划出处：``docs/design/审批与中断恢复优化规划.md`` T30 卡验收②（``:1032``）与
新登记的 T30-3 台账行（``:1163``）。主口径按台账行定死的说法执行：

    同一 KB 检索意图（固定输入 + 判定源取 T29 卡 ④.1）在 flag-off / flag-on 下的
    **LLM 调用次数差值**，计数源为假 LLM 计数，刻意不依赖 ``evals/perf_stats.py``。

**链路是真的**（``bench/README.md`` 铁律 1：不复制被测逻辑）：本文件只 import + 调用，
逐跳都走生产实现——

* L1：``agentix.application.agents.router.analyze_and_route_query``（含 ``_exec_flags`` /
  ``_router_class`` / ``_router_prompt_text`` / ``sanitized_kwargs`` 带参出图）；
* 分派：``agentix.application.agents.router.route_query``（真实节点名判定，本文件不自己按
  ``type`` 写 if-else）；
* L2：``agentix.application.agents.general_handler.create_kb_query``（真实转发判据
  ``"kb_route" in type(router).model_fields``）→ 真实 KB 子图
  ``workflows/multi_agent/kb_workflow.py::create_kb_multi_tool_workflow``
  （guardrails → kb_router(A3) → local_search → finalize）；
* 对照组走真实 ``general_handler.respond_to_general_query``。

图的状态合并由本文件手工完成（``state.router = update["router"]``）——真实合并点是
LangGraph 的 reducer，本文件不起主图（主图会拉进 checkpointer / Neo4j / 检索后端等
一整套 I/O，与本条读数无关）。

**桩只打在三个位置**（每个都在「实例化 / I/O」层，不在判定层）：

1. ``router`` 与 ``general_handler`` 两个模块的 ``get_llm_client`` ⇒ 链路上每个 chat
   model 换成**同一个**计数假模型（``_CountingChatModel``）。它支持
   ``with_structured_output(schema)`` 与 ``ainvoke``，按 schema 回放**合法固定值**。
2. ``general_handler.KnowledgeGateway`` ⇒ 返回两条固定文档的假检索（真 ``local_search``
   节点照跑，``kb_retrieval.hybrid_retrieve`` 照跑）。**必须有结果**：``finalize_node.py``
   在空结果时提前返回、不调模型（``:70-77``），那样量到的跳数不是 T29 块 B 的「典型」形态。
3. ``agentix.application.services.observability.get_observability`` ⇒ 空实现。理由：
   ``kb_retrieval._record_rag_retrieval`` 会往 Redis 写埋点（宿主 ``.env`` 的
   ``REDIS_URL=redis://redis:6379/0`` 在本机不可达），而埋点不属判跳链路。

另有两条**硬闸**（不是桩，是防止桩打漏）：

* ``aiohttp.ClientSession`` 被换成一调用就抛 ``RuntimeError`` 的哨兵 ⇒ 任何试图发 HTTP
  的路径当场炸，不会静默出网；
* ``general_handler.create_knowledge_query_node`` 被换成抛错的哨兵 ⇒ 它是
  ``create_kb_query`` 的**降级出口**，真链上一旦走到那里就说明样本已不成立，
  读数必须作废而不是继续计数（本文件不许用降级样本冒充基线）。

**离线承诺**：绝不联网、绝不调真实模型；``LLM_BASE_URL`` / API key 在本文件里根本不被读取。

用法::

    .venv/Scripts/python.exe -m bench.single_layer_jump_count

输出：每个 arm 实际生效的 flag 取值、每次模型调用的**调用点**（``file:line func``，
由栈回溯取得，不是本文件自己标的）、两档整数计数与差值、缓存 miss/hit 两档、
非 kb-query 对照组、以及与 T29 块 B 的校准对照。

**不承诺**：本文件不下质量结论、不改任何默认值、不落盘产物、不是 T30 卡的「复验命令」
里那套 bench 参照口径（时延/分位数/指纹）——参照口径的前置 ``evals/perf_stats.py``
未跟踪，见 ``docs/design/审批与中断恢复优化规划.md`` T30 行待追认②。
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:  # 兼容非 -m 方式直跑
    sys.path.insert(0, _REPO_ROOT)

import aiohttp  # noqa: E402  ← 仅用于装「禁止出网」哨兵
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from langchain_core.runnables import Runnable  # noqa: E402

from agentix.application.agents import general_handler as general_handler_mod  # noqa: E402
from agentix.application.agents import router as router_mod  # noqa: E402
from agentix.application.agents import routing_cache  # noqa: E402
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_states import (  # noqa: E402
    KBGuardrailsDecision,
    KBRouteDecision,
)
from agentix.application.services import observability as observability_mod  # noqa: E402
from agentix.config import ablation, settings  # noqa: E402
from agentix.observability import spans as spans_mod  # noqa: E402
from agentix.orchestration.langgraph.lg_states import (  # noqa: E402
    AgentState,
    Router,
)

TARGET_NAME = "single_layer_jump_count"

# ---------------------------------------------------------------------------
# T29 卡 ④.1 的固定输入（逐字抄自 docs/design/审批与中断恢复优化规划.md:829-831，
# general-query 行实测在 :829、kb-query 行实测在 :831）
# ---------------------------------------------------------------------------

#: kb-query 固定输入 + 预期意图（T29 ④.1 表第 3 行）
KB_QUERY_INPUT = "公司考勤制度怎么规定的？"
KB_QUERY_EXPECTED_DECISION = "kb-query"

#: general-query 固定输入 + 预期意图（T29 ④.1 表第 1 行，非 kb 对照组）
GENERAL_QUERY_INPUT = "你好，介绍一下你自己"
GENERAL_QUERY_EXPECTED_DECISION = "general-query"

#: T29 块 B（:794）kb-query 行的 (最小, 典型, 上限) —— 校准基准，逐字取自文档
T29_KB_BLOCK_B = (
    2,
    4,
    "4（AGENTIC_RAG_ENABLED=false 那一档）/ 12（=true 时每轮 +1 改写且重跑子图 +3，≤2 轮）",
)
#: T29 块 B（:792）general-query 行的 (最小, 典型, 上限)
T29_GENERAL_BLOCK_B = (2, 2, 2)

#: 两臂共用、且必须全程固定的其余开关（对齐 T29 ④.1 前置表 + test_p176 的
#: ``_clean_routing_flags`` 口径：这些键任一翻开都会让某些跳直接消失或多出来）
CONSTANT_FLAGS: List[tuple[str, Any]] = [
    ("ROUTING_FASTPATH_ENABLED", False),
    ("TASK_EXEC_ENABLED", False),
    ("CODE_EXEC_ENABLED", False),
    ("AGENTIC_RAG_ENABLED", False),
    ("CLARIFY_ROUTING_ENABLED", False),
    ("KB_ENABLE_EXTERNAL_SEARCH", False),
    ("KG_PLANNER_SHADOW_ENABLED", False),
    ("KG_FINAL_ANSWER_VALIDATION", False),
    ("KB_LOCAL_FUSION_ENABLED", False),
    ("TOOL_SELECTION_ENABLED", False),
    ("LG_INSTRUCTIONS_ENABLED", False),
    ("MEMORY_CONTEXT_ENABLED", False),
    ("LG_STATUS_TAIL_ENABLED", False),
]

#: 打印用：本路径上实测会改变跳数的开关（含臂变量）
ARMOORED_FLAGS = (
    "ROUTING_SINGLE_LAYER_ENABLED",
    "ROUTING_CACHE_ENABLED",
)

#: 假模型对 KB 子图 A3 / L1 单层的固定通道（两臂同一通道，只差在「谁决定的」）
FIXED_KB_ROUTE = "local"
FIXED_KB_TOOLS = ["milvus"]  # 刻意不含 postgres：postgres 通道走 aiohttp，本机不许出网

_RUNNABLE_CONFIG: Dict[str, Any] = {"configurable": {}}

_AGENTIX_DIR = os.path.normpath(os.path.join(_REPO_ROOT, "agentix"))


# ---------------------------------------------------------------------------
# 调用点归因
# ---------------------------------------------------------------------------


def _call_frame_site() -> str:
    """沿栈回溯找**最近的 agentix 生产帧**，返回 ``相对路径:行号 函数名``。

    读数要能复核到站点（简报要求），因此归因优先取真实调用栈，而不是本文件自己标的标签。
    跳过 ``llm_factory``（假模型已把它的 ``_TrackedChatOpenAI`` 整层换掉，留在栈里只会
    指向桩位）与本文件自身。

    **KB 子图内的两次结构化调用拿不到帧**：LangGraph 把节点协程跑在自己创建的 task 里，
    栈不跨 task（实测显示为 ``<跨 task：改用 config 归因>``）。那不是缺口——框架会把
    节点名经 ``config`` 传进 ``ainvoke``，见 :func:`_graph_node_from_config`。
    """
    frame: Optional[inspect.FrameInfo] = inspect.currentframe()
    while frame is not None:
        raw = frame.f_code.co_filename
        norm = os.path.normpath(raw)
        if (
            _AGENTIX_DIR in norm
            and "llm_factory" not in norm
            and os.path.normpath(__file__) != norm
        ):
            try:
                rel = os.path.relpath(norm, _REPO_ROOT)
            except ValueError:  # pragma: no cover - 跨盘根时退回绝对路径
                rel = norm
            return f"{rel}:{frame.f_lineno} {frame.f_code.co_name}"
        frame = frame.f_back  # type: ignore[assignment]
    return "<跨 task：改用 config 归因>"


def _graph_node_from_config(config: Any) -> str:
    """从 LangGraph 注入的 ``config`` 里取**框架自己记的**节点名。"""
    if not isinstance(config, dict):
        return "-"
    metadata = config.get("metadata") or {}
    configurable = config.get("configurable") or {}
    node = (
        metadata.get("langgraph_node")
        or configurable.get("__langgraph_node")
        or configurable.get("langgraph_node")
    )
    return str(node) if node else "-"


#: ``schema(+节点) → 生产调用点``：把归因**核到代码行**的静态对照表。
#: 本表不参与计数（计数只认 ``_produce`` 那一次记账），只用于让审查者按行号复核。
STATIC_CALL_POINTS: Dict[str, str] = {
    "Router": "agentix/application/agents/router.py:513（L1 with_structured_output(router_cls).ainvoke）",
    "SingleLayerRouter": "agentix/application/agents/router.py:513（同上，单层变体 schema）",
    "KBGuardrailsDecision@guardrails": (
        "agentix/application/agents/kg_sub_graph/agentic_rag_agents/workflows/"
        "multi_agent/kb_nodes/guardrails_node.py:39（guardrails_chain.ainvoke）"
    ),
    "KBRouteDecision@kb_router": (
        "agentix/application/agents/kg_sub_graph/agentic_rag_agents/workflows/"
        "multi_agent/kb_nodes/router_node.py:54（A3 自判：router_chain.ainvoke）"
    ),
    "plain ainvoke@finalize": "agentix/application/agents/kg_sub_graph/agentic_rag_agents/workflows/multi_agent/kb_nodes/finalize_node.py:79（llm.ainvoke）",
    "plain ainvoke@respond_to_general_query": "agentix/application/agents/general_handler.py:138（model.ainvoke）",
}


# ---------------------------------------------------------------------------
# 计数假模型
# ---------------------------------------------------------------------------


class _RecordingModel:
    """一次模型调用的记账记录。"""

    def __init__(self, schema: str, frame_site: str, graph_node: str, phase: str) -> None:
        self.schema = schema
        self.frame_site = frame_site
        self.graph_node = graph_node
        self.phase = phase

    @property
    def key(self) -> str:
        """静态对照表的键：能定出节点就带节点。"""
        return f"{self.schema}@{self.graph_node}" if self.graph_node != "-" else self.schema

    def as_line(self, idx: int) -> str:
        static = STATIC_CALL_POINTS.get(self.key)
        if static is None:
            # 静态表没覆盖到的（本文件里只有 L1 与 KB 子图两类）退回栈归因——
            # 栈拿得到的帧本身就是生产调用点，不需要另编一份映射（纪律 6 ④）。
            static = f"栈归因：{self.frame_site}"
        return (
            f"      #{idx} schema={self.schema:<20} 帧={self.frame_site:<62} "
            f"langgraph节点={self.graph_node:<22} 阶段={self.phase}\n"
            f"           → 生产调用点：{static}"
        )


class _CountingChatModel:
    """链路上唯一被使用的 chat model 桩：按 schema 回放合法固定值，并逐次记账。

    ``with_structured_output`` **不记账**——KB 子图里它在节点工厂构造期就被调用
    （``router_node.py:39``、``guardrails_node.py:35``），记账会把它误算成一跳；
    真正花钱的是返回链的 ``ainvoke``（同 ``tests/test_p176_routing_single_layer.py``
    的 ``_CountingKBLLM`` 实测结论）。
    """

    def __init__(self) -> None:
        self.records: List[_RecordingModel] = []
        self.phase = "unset"
        self.route_decisions: List[str] = []

    # -- Runnable 形态：既要在 L1 被直接 ``.ainvoke``，也要能进 ``prompt | chain`` --
    def with_structured_output(self, schema: Any, **_kwargs: Any) -> Runnable:
        return _StructuredCall(self, schema)

    async def ainvoke(self, _input: Any = None, config: Any = None, **_kwargs: Any) -> AIMessage:
        return self._produce(None, config=config)

    # -- 产出与记账（唯一计数源） --
    def _produce(self, schema: Optional[Any], *, config: Any = None) -> Any:
        frame_site = _call_frame_site()
        graph_node = _graph_node_from_config(config)
        phase = self.phase
        self.records.append(_RecordingModel(_schema_name(schema), frame_site, graph_node, phase))
        if schema is None:
            return AIMessage(content="bench: 假模型固定答复（不计入路由决策，只计内容生成一跳）")
        return _fixed_output_for(schema)


def _schema_name(schema: Optional[Any]) -> str:
    if schema is None:
        return "plain ainvoke"
    return getattr(schema, "__name__", str(schema))


def _fixed_output_for(schema: Any) -> Any:
    """按 schema 造**合法**固定值；认不出的 schema 直接抛，不做静默兜底。"""
    if isinstance(schema, type) and issubclass(schema, Router):
        fields = getattr(schema, "model_fields", {})
        payload: Dict[str, Any] = {
            "type": _INTENT_BY_QUESTION.get() or "kb-query",
            "logic": "bench: fixed classifier output",
            "question": "",
            "decision": None,
            "confidence": None,
            "reasoning": None,
        }
        if "kb_route" in fields:
            # 单层变体：L1 必须真的把通道参数带出去，否则 A3 拿不到转发参数
            payload["kb_route"] = FIXED_KB_ROUTE
            payload["kb_tools"] = list(FIXED_KB_TOOLS)
        return schema(**payload)
    if schema is KBGuardrailsDecision:
        return KBGuardrailsDecision(
            decision="proceed", summary="bench: 在范围内", rationale="bench"
        )
    if schema is KBRouteDecision:
        return KBRouteDecision(route=FIXED_KB_ROUTE, rationale="bench", tools=list(FIXED_KB_TOOLS))
    raise RuntimeError(f"取证脚本没有为 schema {schema!r} 定义合法固定输出——样本作废，不猜默认值。")


class _StructuredCall(Runnable):
    """``model.with_structured_output(cls)`` 的返回链：同步/异步两个入口共用一份记账。"""

    def __init__(self, model: _CountingChatModel, schema: Any) -> None:
        self._model = model
        self._schema = schema

    def invoke(self, _input: Any = None, config: Any = None, **_kwargs: Any) -> Any:
        return self._model._produce(self._schema, config=config)

    async def ainvoke(self, _input: Any = None, config: Any = None, **_kwargs: Any) -> Any:
        return self._model._produce(self._schema, config=config)


#: 假模型在 L1 那次调用里该产哪个意图（由场景决定；线程内单点设置）
class _IntentSlot:
    value = "kb-query"

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


_INTENT_BY_QUESTION = _IntentSlot()


# ---------------------------------------------------------------------------
# 其余桩位（I/O 层）
# ---------------------------------------------------------------------------


class _StubReranker:
    enabled = False

    async def rerank(self, *_args: Any, **_kwargs: Any) -> List[Dict[str, Any]]:  # pragma: no cover
        raise AssertionError("bench：假网关不该触发 rerank（enabled=False）")


class _StubKnowledgeGateway:
    """真 ``local_search`` 节点的检索后端桩：两条固定文档，保证 finalize 走「有结果」支路。"""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.reranker = _StubReranker()
        self.search_calls = 0

    async def search(self, **kwargs: Any) -> List[Dict[str, Any]]:
        self.search_calls += 1
        return [
            {
                "id": "bench-doc-1",
                "source": "bench://attendance#1",
                "content": "考勤制度：工作日 9:00 上班，18:00 下班，弹性 30 分钟。",
                "similarity": 0.93,
                "metadata": {"source": "bench://attendance"},
            },
            {
                "id": "bench-doc-2",
                "source": "bench://attendance#2",
                "content": "迟到 3 次折算 1 天事假；出差与外勤按打卡记录核定。",
                "similarity": 0.88,
                "metadata": {"source": "bench://attendance"},
            },
        ]


class _NoNetworkSession:
    """``aiohttp.ClientSession`` 哨兵：任何 HTTP 出口即抛 ⇒ 桩打漏了会当场炸，不会静默出网。"""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(
            "bench.single_layer_jump_count 不许发起任何 HTTP 请求"
            "（出现本错误说明有 I/O 桩没打上，读数作废）"
        )


class _NoOpObservability:
    async def record_rag_retrieval(self, **_kwargs: Any) -> None:
        return None

    async def record_llm_call(self, **_kwargs: Any) -> None:  # pragma: no cover - 兜底
        return None


def _explode_on_fallback(*_args: Any, **_kwargs: Any):
    """``create_knowledge_query_node`` 哨兵：真链的降级出口一旦被走到就抛，读数作废。"""

    async def _node(_state: Any) -> Any:
        raise RuntimeError(
            "bench：create_kb_query 走了降级出口（多工具工作流抛错）⇒ 该样本不成立，"
            "不许用降级路径的跳数冒充基线。"
        )

    return _node


# ---------------------------------------------------------------------------
# flag 注入与打印
# ---------------------------------------------------------------------------


def _host_values(keys: List[str]) -> Dict[str, Any]:
    return {k: getattr(settings, k, "<无此键>") for k in keys}


def _apply_arm_flags(
    *, single_layer: bool, cache: bool, constants: Dict[str, Any]
) -> Dict[str, Any]:
    """以 ``setattr(settings, ...)`` 显式覆盖（**不改 .env**，不新实例化 Settings）。"""
    effective: Dict[str, Any] = {}
    for key, value in constants:
        setattr(settings, key, value)
        effective[key] = getattr(settings, key)
    settings.ROUTING_SINGLE_LAYER_ENABLED = single_layer
    settings.ROUTING_CACHE_ENABLED = cache
    effective["ROUTING_SINGLE_LAYER_ENABLED"] = settings.ROUTING_SINGLE_LAYER_ENABLED
    effective["ROUTING_CACHE_ENABLED"] = settings.ROUTING_CACHE_ENABLED
    return effective


def _install_stubs(model: _CountingChatModel) -> Dict[str, str]:
    """将桩位挂到生产模块上，返回「桩位 → 理由」用于打印。"""
    router_mod.get_llm_client = lambda **_kwargs: model
    general_handler_mod.get_llm_client = lambda **_kwargs: model
    general_handler_mod.KnowledgeGateway = _StubKnowledgeGateway
    general_handler_mod.create_knowledge_query_node = _explode_on_fallback
    observability_mod.get_observability = lambda: _NoOpObservability()
    spans_mod.record_route_decision = _spy_route_decision(model)
    router_mod.record_route_decision = spans_mod.record_route_decision
    aiohttp.ClientSession = _NoNetworkSession  # type: ignore[misc]
    return {
        "router.get_llm_client / general_handler.get_llm_client": "换成同一个计数假模型（桩在实例化层）",
        "general_handler.KnowledgeGateway": "检索 I/O 桩（真 local_search 节点与 hybrid_retrieve 照跑）",
        "general_handler.create_knowledge_query_node": "抛错哨兵：降级出口一旦触发即样本作废",
        "observability.get_observability": "空实现：避免往宿主 .env 的 Redis 写埋点",
        "router.record_route_decision": "记账探针（同一参数转发真 helper）：取 span 属性本会写到的值",
        "aiohttp.ClientSession": "抛错哨兵：绝不联网",
    }


def _spy_route_decision(model: _CountingChatModel):
    """在**唯一写入点**上取样：记下的就是 ``agentix.route.decision`` 会拿到的值。"""
    real = spans_mod.record_route_decision

    def spy(route_type: Any) -> None:
        model.route_decisions.append(str(route_type))
        real(route_type)

    return spy


# ---------------------------------------------------------------------------
# 采样：一次完整问答（真 L1 → 真 route_query → 真 L2 节点）
# ---------------------------------------------------------------------------


async def _one_request(
    model: _CountingChatModel,
    *,
    question: str,
    intent: str,
    node_hint: str,
    reset_cache: bool = True,
) -> Dict[str, Any]:
    if reset_cache:
        routing_cache.reset_routing_cache()
    model.records.clear()
    model.route_decisions.clear()
    _INTENT_BY_QUESTION.set(intent)

    state = AgentState(messages=[HumanMessage(content=question)])
    model.phase = "L1 analyze_and_route_query"
    update = await router_mod.analyze_and_route_query(state, config=_RUNNABLE_CONFIG)
    # 手工完成真实图的状态合并（合并点在 lg_builder 的 reducer）：L1 返回的是增量 dict
    state.router = update["router"]
    decision = model.route_decisions[-1] if model.route_decisions else "<未写入（兜底出口）>"

    node_name = router_mod.route_query(state)
    node_fn = getattr(general_handler_mod, node_name)
    model.phase = node_hint
    result = await node_fn(state, config=_RUNNABLE_CONFIG)

    messages = result.get("messages") if isinstance(result, dict) else None
    answer = getattr(messages[-1], "content", "") if messages else ""
    return {
        "decision": decision,
        "dispatched_node": node_name,
        "model_calls": len(model.records),
        "records": list(model.records),
        "answer_head": (answer or "")[:40],
    }


async def run_scenarios() -> Dict[str, Any]:
    model = _CountingChatModel()
    stub_notes = _install_stubs(model)
    constants = list(CONSTANT_FLAGS)

    host = _host_values([k for k, _v in constants] + list(ARMOORED_FLAGS))

    out: Dict[str, Any] = {
        "target": TARGET_NAME,
        "host_env_values": host,
        "ablation": {
            "router_enabled": ablation.is_enabled("router_enabled"),
            "retrieval_enabled": ablation.is_enabled("retrieval_enabled"),
            "multi_agent": ablation.is_enabled("multi_agent"),
        },
        "stubs": stub_notes,
        "samples": {},
    }

    # ---- 主口径：ROUTING_CACHE_ENABLED=false，kb-query 两臂 ----
    for label, single_layer in (("flag_off", False), ("flag_on", True)):
        key = f"primary_{label}"
        eff = _apply_arm_flags(single_layer=single_layer, cache=False, constants=constants)
        sample = await _one_request(
            model,
            question=KB_QUERY_INPUT,
            intent=KB_QUERY_EXPECTED_DECISION,
            node_hint="L2 create_kb_query（真 KB 子图）",
        )
        sample["effective_flags"] = eff
        sample["void"] = _void_reasons(
            sample,
            expected_decision=KB_QUERY_EXPECTED_DECISION,
            expect_a3_short_circuit=single_layer,
        )
        out["samples"][key] = sample

    # ---- ③：ROUTING_CACHE_ENABLED=true，两臂各跑 miss + hit（hit 不重置缓存） ----
    for label, single_layer in (("flag_off", False), ("flag_on", True)):
        eff = _apply_arm_flags(single_layer=single_layer, cache=True, constants=constants)
        miss = await _one_request(
            model,
            question=KB_QUERY_INPUT,
            intent=KB_QUERY_EXPECTED_DECISION,
            node_hint="L2 create_kb_query（真 KB 子图）",
        )
        miss["effective_flags"] = eff
        miss["void"] = _void_reasons(
            miss,
            expected_decision=KB_QUERY_EXPECTED_DECISION,
            # 未命中 ⇒ L1 照常产参 ⇒ A3 应短路（单层臂）
            expect_a3_short_circuit=single_layer,
        )
        # 第二次同问：缓存里已有第一次成功写入的四字段条目（不含单层通道参数）
        hit = await _one_request(
            model,
            question=KB_QUERY_INPUT,
            intent=KB_QUERY_EXPECTED_DECISION,
            node_hint="L2 create_kb_query（真 KB 子图）",
            reset_cache=False,
        )
        hit["effective_flags"] = eff
        hit["void"] = _void_reasons(
            hit,
            expected_decision=KB_QUERY_EXPECTED_DECISION,
            # 命中路径不带单层参数 ⇒ 两臂都应当 A3 自调模型
            expect_a3_short_circuit=False,
        )
        out["samples"][f"cache_on_{label}_miss"] = miss
        out["samples"][f"cache_on_{label}_hit"] = hit

    # ---- ④：非 kb-query 对照组（general-query），cache=false ----
    for label, single_layer in (("flag_off", False), ("flag_on", True)):
        eff = _apply_arm_flags(single_layer=single_layer, cache=False, constants=constants)
        sample = await _one_request(
            model,
            question=GENERAL_QUERY_INPUT,
            intent=GENERAL_QUERY_EXPECTED_DECISION,
            node_hint="L2 respond_to_general_query",
        )
        sample["effective_flags"] = eff
        sample["void"] = _void_reasons(
            sample,
            expected_decision=GENERAL_QUERY_EXPECTED_DECISION,
            expect_a3_short_circuit=None,
        )
        out["samples"][f"general_{label}"] = sample

    return out


def _void_reasons(
    sample: Dict[str, Any],
    *,
    expected_decision: str,
    expect_a3_short_circuit: Optional[bool],
) -> List[str]:
    """样本成立性的前置断言（本文件自己的读数前提，不是对被测逻辑的重写）。

    ``expect_a3_short_circuit``：
    * ``True``  ⇒ 单层臂（cache 关）：A3 必须短路，仍调模型即"参数没送到子图"；
    * ``False`` ⇒ 基线臂 / 缓存命中臂：A3 必须自调一次模型；
    * ``None``  ⇒ 非 KB 臂，不判这条。
    """
    reasons: List[str] = []
    if sample["decision"] != expected_decision:
        reasons.append(f"意图先验校验失败：决策值={sample['decision']} ≠ 预期 {expected_decision}")
    schemas = [r.schema for r in sample["records"]]
    a3_called = "KBRouteDecision" in schemas
    if expected_decision == "kb-query":
        if "plain ainvoke" not in schemas:
            reasons.append(
                "finalize 未调模型（走的是空结果提前返回支路）⇒ 不是 T29 块 B 的「典型」形态"
            )
        if expect_a3_short_circuit is True and a3_called:
            reasons.append("flag-on 但 A3 仍自调模型 ⇒ 单层参数没送到子图，单层化未落地")
        if expect_a3_short_circuit is False and not a3_called:
            reasons.append("预期 A3 自调模型（基线 / 缓存命中）但样本里没有 KBRouteDecision 调用")
    return reasons


# ---------------------------------------------------------------------------
# 打印
# ---------------------------------------------------------------------------


def _fmt_sample(name: str, sample: Dict[str, Any]) -> List[str]:
    lines = [
        f"  [{name}]",
        f"    生效 flag：{json.dumps(sample['effective_flags'], ensure_ascii=False)}",
        f"    决策值（record_route_decision 取样，即 agentix.route.decision 的写入值）= {sample['decision']}",
        f"    实走节点（route_query 判定）= {sample['dispatched_node']}",
        f"    模型调用次数 = {sample['model_calls']}",
        "    逐次调用点：",
    ]
    for idx, rec in enumerate(sample["records"], start=1):
        lines.append(rec.as_line(idx))
    lines.append(f"    答复前 40 字：{sample['answer_head']!r}")
    if sample.get("void"):
        lines.append(f"    ⚠ 样本作废原因：{sample['void']}")
    return lines


def _report(out: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("=" * 78)
    lines.append(f"T30-3 单层路由跳数取证：{TARGET_NAME}")
    lines.append("=" * 78)
    lines.append("")
    lines.append("[0] 运行前提")
    lines.append(f"    Python {sys.version.split()[0]}；仓库根 {_REPO_ROOT}")
    lines.append(
        "    flag 注入方式：setattr(agentix.config.settings.settings, ...) 显式覆盖"
        "（不新建 Settings 实例、不改 .env）"
    )
    lines.append(
        "    ablation 生效值："
        + json.dumps(out["ablation"], ensure_ascii=False)
        + "  （router/retrieval 任一为 False 都会让整条跳链变形）"
    )
    lines.append(
        "    计跳判据：本文件 _produce 的一次记账 = 一次真实模型调用（台账 T30-3 行的「假 LLM 计数」）。"
    )
    lines.append(
        "    意图先验校验判据：router.py:567（LLM 路径）与 router.py:407（缓存命中路径）"
        "两处写入点 record_route_decision 的实参取样（对齐 T29 ④.1「唯一写入点（缓存命中路径另列）」"
        "的口径——同一个函数、两个调用点，spy 挂在模块属性上两处都经过）；"
        "T29 ④.1 写的 span 属性 agentix.route.decision 在进程内不起 trace 导出时取不到"
        "（spans.py:149-155 无 span 即静默 no-op），故取**同一写入点的入参**作等价判据——"
        "不是另立第二份判据，值与写入值同源。"
    )
    lines.append("")
    lines.append("[1] 宿主 .env 给 settings 的原值（本脚本不采信，仅登记）")
    for key, value in out["host_env_values"].items():
        lines.append(f"    {key} = {value!r}")
    lines.append("")
    lines.append("[2] 桩位（全部在实例化 / I/O 层，判定层零复制）")
    for site, note in out["stubs"].items():
        lines.append(f"    - {site}：{note}")
    lines.append("")
    lines.append(
        "[3] 主口径：ROUTING_CACHE_ENABLED=false，固定输入 "
        f"{KB_QUERY_INPUT!r}（T29 ④.1 kb-query 行）"
    )
    off = out["samples"]["primary_flag_off"]
    on = out["samples"]["primary_flag_on"]
    for name, sample in (("flag_off", off), ("flag_on", on)):
        lines.extend(_fmt_sample(name, sample))
    diff = off["model_calls"] - on["model_calls"]
    lines.append("")
    lines.append(
        f"    ⇒ 差值 = flag_off {off['model_calls']} − flag_on {on['model_calls']} = {diff}"
    )
    lines.append("")
    lines.append("[4] ③ ROUTING_CACHE_ENABLED=true（同问两次：miss / hit），量化缓存缺口")
    for arm in ("flag_off", "flag_on"):
        for phase in ("miss", "hit"):
            name = f"cache_on_{arm}_{phase}"
            sample = out["samples"][name]
            lines.append(
                f"    {arm} / {phase}: 次数={sample['model_calls']} "
                f"决策值={sample['decision']} 节点={sample['dispatched_node']} "
                f"调用序列（schema@langgraph节点）={[r.key for r in sample['records']]}"
            )
            if sample.get("void"):
                lines.append(f"      ⚠ {sample['void']}")
    lines.append("")
    lines.append(
        "[5] ④ 非 kb-query 对照组：固定输入 "
        f"{GENERAL_QUERY_INPUT!r}（T29 ④.1 general-query 行，预期差值 0）"
    )
    g_off = out["samples"]["general_flag_off"]
    g_on = out["samples"]["general_flag_on"]
    for name, sample in (("flag_off", g_off), ("flag_on", g_on)):
        lines.extend(_fmt_sample(f"general {name}", sample))
    g_diff = g_off["model_calls"] - g_on["model_calls"]
    lines.append(f"    ⇒ 差值 = {g_off['model_calls']} − {g_on['model_calls']} = {g_diff}")
    lines.append("")
    lines.append(
        "[6] 校准：与 T29 块 B kb-query 行 (最小, 典型, 上限) = "
        f"{T29_KB_BLOCK_B[0]} / {T29_KB_BLOCK_B[1]} / {T29_KB_BLOCK_B[2]} 对照"
    )
    lines.append(
        f"    本次 flag-off 实测总跳数 = {off['model_calls']}"
        "（AGENTIC_RAG_ENABLED=false ⇒ 上限取 4 那一档）"
    )
    if off["model_calls"] == T29_KB_BLOCK_B[1]:
        lines.append(
            "    ⇒ 与「典型 4」逐跳对齐（L1 1 + KB guardrails 1 + kb_router/A3 1 + finalize 1）。"
        )
    else:
        lines.append(
            f"    ⇒ 与「典型 {T29_KB_BLOCK_B[1]}」**不吻合**；逐站点对账见 [3] 的调用点列表，"
            "差异定位需按站点说明（不为了让数对得上而改输入或判据）。"
        )
    lines.append("")
    lines.append("[7] 判定文本（只能由上面的数字得出）")
    verdict = (
        f"kb-query（固定输入 {KB_QUERY_INPUT!r}，ROUTING_CACHE_ENABLED=false）："
        f"flag-off {off['model_calls']} 次 → flag-on {on['model_calls']} 次，"
        f"差值 = {diff}；{'严格小于成立' if diff > 0 else '严格小于不成立'}"
        f"（无作废样本 {'是' if not (off.get('void') or on.get('void')) else '否'}）。"
    )
    lines.append(f"    {verdict}")
    lines.append(
        f"    对照组 general-query：flag-off {g_off['model_calls']} 次 → flag-on {g_on['model_calls']} 次，"
        f"差值 = {g_diff}（预期 0）。"
    )
    lines.append("")
    lines.append(
        "[8] 参照口径（§4 卡复验命令里的 bench 两轮：重复 ≥5、跨轮池化、同一 fingerprint）"
    )
    lines.append(
        "    缺位。原因：本仓既有 bench 骨架的分位数实现落在未跟踪文件 evals/perf_stats.py"
        "（bench/aggregate.py → evals.perf_stats），且 bench/adaptive_ab.py 的臂都要真模型端点"
        "（回环地址闸 + LM Studio）。本任务不许改 evals/、不许提交它，也不许联网调模型 ⇒ "
        "参照口径不能出数。属维护者待追认项（T30 行待追认②）。"
    )
    lines.append(
        "    本文件因此**不落 bench/results/ 产物**、不采集 fingerprint（无时延读数可指纹化）。"
    )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    # Windows 控制台默认 GBK，中文读数与 ⇒/→ 会当场编码失败 ⇒ 显式改 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="T30-3 单层路由跳数取证")
    parser.add_argument("--json", action="store_true", help="额外以 JSON 打印原始记录")
    args = parser.parse_args(argv)

    out = asyncio.run(run_scenarios())
    print(_report(out))
    if args.json:
        printable = {
            k: {
                "decision": v["decision"],
                "dispatched_node": v["dispatched_node"],
                "model_calls": v["model_calls"],
                "records": [
                    {
                        "schema": r.schema,
                        "frame_site": r.frame_site,
                        "langgraph_node": r.graph_node,
                        "static_call_point": STATIC_CALL_POINTS.get(r.key, "未登记"),
                        "phase": r.phase,
                    }
                    for r in v["records"]
                ],
                "void": v.get("void", []),
            }
            for k, v in out["samples"].items()
        }
        print()
        print("[JSON] " + json.dumps(printable, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
