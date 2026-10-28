"""
LangGraph agent builder - backward-compatible re-export layer.

This module preserves all existing import paths while the actual implementations
have been moved to dedicated modules:
  - router.py: analyze_and_route_query, route_query, _heuristic_router, etc.
  - guardrails.py: get_additional_info, AdditionalGuardrailsOutput
  - image_handler.py: create_image_query, _generate_image
  - file_handler.py: create_file_query
  - general_handler.py: respond_to_general_query, create_kb_query
  - code_handler.py: create_code_query
  - constants.py: SCOPE_DESCRIPTION, GUARDRAILS_REJECT_MESSAGE, etc.
  - llm_factory.py: get_llm_client

Only create_research_plan and check_hallucinations remain here due to
complex dependencies on kg_sub_graph internals.
"""

from functools import lru_cache
from typing import cast, Any, Dict, List, Optional

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableConfig
from loguru import logger

from agentix.config import ablation, settings
from agentix.observability.spans import record_resume_from_checkpoint
from agentix.observability.metrics import record_graph_resume
from agentix.orchestration.langgraph.graph_config import durability_kwargs, thread_config
from agentix.orchestration.langgraph.lg_states import AgentState, GradeHallucinations
from agentix.orchestration.langgraph.policies import (
    node_error_handler,
    node_timeout,
    readonly_retry_policy,
)
from agentix.application.agents.lg_prompts import (
    CHECK_HALLUCINATIONS,
)
from agentix.application.agents.kg_sub_graph.kg_neo4j_conn import get_neo4j_graph
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.retrievers.cypher_examples.legal_retriever import (
    LegalCypherRetriever,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.multi_tool import (
    create_multi_tool_workflow,
)
from agentix.application.agents.llm_factory import get_llm_client
from agentix.application.agents.router import _ensure_router
from agentix.application.agents.constants import SCOPE_DESCRIPTION
from agentix.application.agents.task_handler import execute_task
from agentix.application.agents.code_handler import create_code_query
from agentix.tools.registry_init import get_active_tools

# Re-export all public symbols from new modules for backward compatibility
from agentix.application.agents.router import (  # noqa: F401
    analyze_and_route_query,
    route_query,
    _heuristic_router,
    _extract_configurable,
    _coerce_to_bool,
)
from agentix.application.agents.guardrails import (  # noqa: F401
    get_additional_info,
    AdditionalGuardrailsOutput,
)
from agentix.application.agents.image_handler import (  # noqa: F401
    create_image_query,
)
from agentix.application.agents.file_handler import (  # noqa: F401
    create_file_query,
)
from agentix.application.agents.general_handler import (  # noqa: F401
    respond_to_general_query,
    create_kb_query,
)


def _extract_legal_sources(cyphers: Any) -> List[Dict[str, Any]]:
    """Extract legal source references (law name + article number) from Cypher results."""
    sources: List[Dict[str, Any]] = []
    seen: set = set()
    for cypher in cyphers or []:
        records = getattr(cypher, "records", None)
        if records is None and isinstance(cypher, dict):
            records = cypher.get("records")
        if isinstance(records, dict) and "result" in records:
            records = records.get("result") or []
        if not isinstance(records, list):
            continue
        for rec in records:
            if not isinstance(rec, dict):
                continue
            law = rec.get("法规") or rec.get("law") or ""
            article = rec.get("条号") or rec.get("article") or ""
            if not law or not article:
                continue
            source_label = f"《{law}》{article}"
            if source_label in seen:
                continue
            seen.add(source_label)
            snippet = rec.get("内容") or rec.get("content_snippet") or ""
            sources.append({
                "type": "graphrag",
                "law": law,
                "article": article,
                "content_snippet": snippet if isinstance(snippet, str) else str(snippet),
                "source": source_label,
            })
    return sources


@lru_cache(maxsize=4)
def _get_compiled_multi_tool_workflow(
    tool_schemas_key: tuple,
    scope_description: Optional[str],
    llm_cypher_validation: bool,
):
    """按配置指纹缓存编译后的 KG 子图（P1-2b）。

    key 只由「影响图结构的参数」构成；llm/graph/retriever/dict 等
    运行时对象在闭包内获取（单例或常量），不参与 key：
    - llm: 编译期构造一次。复用不影响埋点归属——_TrackedChatOpenAI
      上报时 trace_id 从 ContextVar 动态读取（llm_factory.py），并非
      构造时固化
    - graph: get_neo4j_graph() 进程内单例（P1-2a）。建连失败异常向上
      传播（lru_cache 不缓存异常），由 chat.py 既有 except 链兜底，
      下次请求自动重试
    - retriever: LegalCypherRetriever 无状态（纯函数 get_examples），
      随图固化安全
    """
    from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.predefined_cypher.legal_cypher_dict import (
        predefined_cypher_dict,
    )

    return create_multi_tool_workflow(
        # P0-2(b) 未接线（后续项）：此处同一 llm 实例同时服务 KG 子图内的多角色
        # （planner / tool_selection / guardrails / finalize 共用），
        # 需 per-node 注入才能差异化，已列为后续项。
        llm=get_llm_client(temperature=0.7, tags=["research_plan"]),
        graph=get_neo4j_graph(),
        tool_schemas=list(tool_schemas_key),
        predefined_cypher_dict=predefined_cypher_dict,
        cypher_example_retriever=LegalCypherRetriever(),
        scope_description=scope_description,
        llm_cypher_validation=llm_cypher_validation,
    )


async def create_research_plan(
    state: AgentState, *, config: RunnableConfig
) -> Dict[str, List[str] | str]:
    """Graph RAG query node - decomposes queries and runs multi-tool workflow."""
    logger.info("------execute local knowledge base query------")

    # 消融开关（AGENTIX_ABLATION_TOOLS_ENABLED=false）：
    # 不进入 KG 多工具子图（planner → 工具选择 → text2cypher → finalize），
    # 退回直接 LLM 作答。语义 = 「关掉工具调用循环」；这与变体 C_rag_router
    # （router=true / tools=false）想要表达的「有路由与检索通道、但没有工具调用」
    # 一致。复用 general 角色，避免另起一条 LLM 链路导致口径分裂。
    if not ablation.is_enabled("tools_enabled"):
        logger.info(
            "ablation[tools_enabled=False]: skip KG multi-tool subgraph, "
            "fall back to direct LLM answer"
        )
        return await respond_to_general_query(state, config=config)

    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured for research plan generation.")

    # P1-3: 工具 schema 由注册表提供（settings.DISABLED_TOOLS 可裁剪），
    # 新增工具只需改 registry_init.py 一处；tuple 化后兼作 P1-2b 编译缓存 key。
    tool_schemas: tuple = tuple(get_active_tools())

    # P1-2b: 子图按配置指纹缓存编译，消除每请求 Neo4j 建连 + StateGraph
    # 重建开销；llm/graph 运行时对象在工厂闭包内获取，不参与缓存 key。
    multi_tool_workflow = _get_compiled_multi_tool_workflow(
        tool_schemas_key=tool_schemas,
        scope_description=SCOPE_DESCRIPTION,
        llm_cypher_validation=True,
    )

    last_message = state.messages[-1].content if state.messages else ""
    input_state = {
        "question": last_message,
        "data": [],
        "history": [],
        "route_type": _ensure_router(getattr(state, "router", None)).type,
    }

    # P1-4：转发 config 进 KG 子图，使其内部节点的 LLM 事件可冒泡（经白名单过滤，
    # 仅回答类节点外泄；KG 子图的 guardrails/planner/tool_selection 会被过滤掉）。
    response = await multi_tool_workflow.ainvoke(input_state, config=config)

    cyphers = response.get("cyphers", []) or []
    sources = _extract_legal_sources(cyphers)

    ai_message = AIMessage(content=response.get("answer", ""))
    if sources:
        ai_message.additional_kwargs["sources"] = sources

    return {"messages": [ai_message], "sources": sources}


# DEAD-CODE(P2-5)｜处置=保留·有意不接线：本函数**有意不接线**（2026-09 审计确认，非"待删除"）。
# build_graph 的 add_node 块只注册 10 个节点，不含它；全仓除本定义与顶部模块
# 说明外 0 处引用，连带函数体内 get_llm_client(..., role="hallucination")
# 亦落在不可达代码中。
# **为什么不删**：这是**文档化的未接线能力**——`docs/design/编排方案选型论证.md`
# 的节点清点表把「A6 幻觉校验」列为"现有但未接入任何图"并附了 grep 证据；
# 它是被刻意保留的后续接线点，不是无人认领的残骸。删掉等于抹掉一处已登记的设计意图。
# **为什么不接线**：接线会给每次图执行新增一次 hallucination 校验 LLM 调用，
# 属运行期行为变更 + 成本变更，需独立评估（当前环境无 LLM 服务，无法评估）。
# 取舍：保持"标注 + 运行期断言未接线"（绊线见 tests/test_p24_deadcode_marking.py）。
async def check_hallucinations(
    state: AgentState, *, config: RunnableConfig
) -> dict[str, Any]:
    """Check if the response is supported by retrieved documents."""
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured for hallucination checks.")

    # P0-2(b)：显式绑定 hallucination 角色 → 确定性 profile（temperature=0.0,
    # timeout=15s, max_retries=1）。调用点传 temperature=0.7 → 落到 profile 温度 0.0。
    model = get_llm_client(temperature=0.7, tags=["hallucinations"], role="hallucination")

    system_prompt = CHECK_HALLUCINATIONS.format(
        documents=state.documents,
        generation=state.messages[-1],
    )

    messages = [{"role": "system", "content": system_prompt}] + state.messages

    logger.info("---CHECK HALLUCINATIONS---")

    response = cast(
        GradeHallucinations,
        await model.with_structured_output(GradeHallucinations).ainvoke(messages),
    )

    return {"hallucination": response}


# ---------------------------------------------------------------------------
# Backward-compatible graph compilation
# ---------------------------------------------------------------------------
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph
from agentix.orchestration.langgraph.lg_states import InputState


def create_message_window_node(max_messages: int, token_budget: int = 0):
    """批次 7：主图历史窗口化节点工厂。

    背景：``AgentState.messages`` 走 ``add_messages`` reducer，会话经
    checkpointer（thread_id=session_id）跨请求持久化，旧消息**无界累积**，
    长会话会持续推高每轮的上下文体量与延迟。本节点在每轮入口处把
    ``messages`` 裁剪到最近 ``max_messages`` 条（含本轮新输入的 human 消息），
    裁剪通过 ``RemoveMessage``（add_messages 官方删除语义）完成，checkpointer
    会持久化裁剪后的状态。

    安全性：
    - ``max_messages <= 0`` 时为**纯 no-op**（窗口化关闭，行为与旧版一致）；
    - 未超限时同样 no-op；
    - 无 id 的消息跳过删除（add_messages 会为其补 id，正常不出现）；
    - 裁剪只影响「送入模型的历史窗口」，Redis/DB 里的完整会话记录不受影响。

    上下文工程 C（``token_budget = LG_TOKEN_HISTORY_BUDGET``，默认 0 = 关）：
    条数窗口之上再加一道 **token 级预算**——从最新消息**向前一次性累加**
    ``count_tokens``，累到预算即停（``while`` 反向遍历，每条只算一次，
    避免逐条重算的 O(n²)）；**至少保留最新一条消息**（本轮输入不可裁）。
    ``token_budget <= 0`` ⇒ 该分支完全不执行，与旧实现逐位等价。
    """
    from langchain_core.messages import RemoveMessage

    def _text(message) -> str:
        content = getattr(message, "content", "")
        return content if isinstance(content, str) else str(content)

    async def window_messages(state: AgentState) -> Dict[str, Any]:
        if max_messages <= 0 and token_budget <= 0:
            return {"messages": []}
        msgs = state.messages or []
        if not msgs:
            return {"messages": []}
        # ① 条数窗口（旧逻辑）：确定候选窗口起点
        start = 0
        if max_messages > 0 and len(msgs) > max_messages:
            start = len(msgs) - max_messages
        # ② token 预算窗口（C 阶段新增）：从尾部向前累加，超预算即停。
        #    最新一条无论如何保留（至少 i = len(msgs)-1）。
        if token_budget > 0:
            from agentix.agent.context.token_count import count_tokens

            used = count_tokens(_text(msgs[-1]))
            i = len(msgs) - 2
            while i >= start:
                cost = count_tokens(_text(msgs[i]))
                if used + cost > token_budget:
                    break
                used += cost
                i -= 1
            start = max(start, i + 1)
        if start <= 0:
            return {"messages": []}
        victims = [RemoveMessage(id=m.id) for m in msgs[:start] if m.id]
        if not victims:
            return {"messages": []}
        return {"messages": victims}

    return window_messages


def _wrap_execute_task_in_loop(node_fn):
    """F1e·A-P1-4（统一执行链 Stage F）：execute_task 的进环 HITL 注入壳。

    langgraph 依赖**只在本模块**（p66 LangGraph 白名单）：运行态满足
    ① py3.12+（3.10 async ``interrupt`` 必死锁，见
    ``tests/test_p32_tool_guards.py`` 特征化，故 3.10 诚实降级=不注入、
    沿用 review_required 快速失败）、② ``HITL_IN_LOOP_ENABLED``、
    ③ ``HUMAN_REVIEW_ENABLED``、④ 调用方声明进环意图
    （``configurable.hitl_in_loop``，非 stream ask 注入）四条时，把
    ``langgraph.types.interrupt`` 经 config 递给 ``execute_task`` →
    ``task_execution``：遇 review_required 挂起（checkpointer 快照
    thread_id），恢复值 ``approve`` ⇒ 落批准后重试一次，其余 fail-closed。

    T23（流内审批）：config 里带 ``stream_hitl`` 通道时**优先**用它的
    ``request`` 回调替代 ``interrupt``——流只有一条 SSE 响应，图一挂起就没帧可发，
    故流侧改为节点内等待（等待期由流侧循环下发审批帧/心跳帧）；该回调是 async，
    且不依赖 langgraph ``interrupt`` ⇒ **不受 ①的版本门控**。

    flag-off/不满足时透传原 config——与基线逐位等价。
    """
    import sys as _sys

    async def _in_loop_execute_task(
        state: AgentState, *, config: RunnableConfig
    ) -> Dict[str, Any]:
        cfg_map = config if isinstance(config, dict) else dict(config or {})
        configurable = (
            cfg_map.get("configurable")
            if isinstance(cfg_map.get("configurable"), dict)
            else None
        )
        if configurable is not None and configurable.get("hitl_in_loop"):
            callback = None
            stream_channel = configurable.get("stream_hitl")
            if stream_channel is not None:
                # T23：**流**内审批通道优先。流只有一条 SSE 响应可写——图一挂起
                # （interrupt）就没有后续帧，客户端只能等 idle 超时，所以流侧用
                # 「节点内等待」：回调是 async 的，等待期把审批帧/心跳帧交给流侧
                # 循环下发。它不依赖 langgraph 的 async interrupt ⇒ 不受下面
                # py3.12 版本门控（门控只为 interrupt 的死锁问题存在）。
                callback = stream_channel.request
            elif (
                _sys.version_info >= (3, 12)
                and settings.HITL_IN_LOOP_ENABLED
                and settings.HUMAN_REVIEW_ENABLED
            ):
                from langgraph.types import interrupt

                callback = interrupt
            if callback is not None:
                cfg = dict(cfg_map)
                new_configurable = dict(configurable)
                # 通道本体不往节点里带（节点只需要回调）：config 会被框架与
                # 追踪层一路传下去，少挂一个本层内部对象少一处意外。
                new_configurable.pop("stream_hitl", None)
                new_configurable["hitl_in_loop_interrupt"] = callback
                cfg["configurable"] = new_configurable
                return await node_fn(state, config=cfg)
        return await node_fn(state, config=config)

    return _in_loop_execute_task


async def resume_in_loop(thread_id: str, resume_value: Any) -> Any:
    """F1e·A-P1-4：恢复图内挂起的进环 HITL 线程（/ops 恢复面唯一调用方）。

    langgraph ``Command`` 依赖留本模块（p66 白名单）；``graph`` 迟绑定——
    与 ops_router ``/threads/resume`` 同款语义，启动期 Redis checkpointer
    覆盖后自动跟随最新图。``resume_value`` 原样交给 ``interrupt`` 返回处：
    ``approve`` 落批准后重试，其余值 fail-closed 快速失败。线程无挂起
    （未知 thread / 未开进环）时由图侧抛错，调用方转 409。
    """
    from langgraph.types import Command

    # LG-2-3：config 形状（含顶层 recursion_limit）只从 graph_config 出——恢复路径
    # 此前缺这个键，会落到框架默认 25，与主链路的 MAX_ITERATIONS 各说一套。
    config = thread_config(thread_id)
    # PhaseK 任务 6：进环恢复点落 span。thread_id 判据只从 config 取（唯一真源，
    # 纪律 6 ④）；fail-open 由 helper 保证（无 span/异常 ⇒ no-op，纪律 2）。
    record_resume_from_checkpoint(thread_id_present=bool(config["configurable"].get("thread_id")))
    # LG-3-3：第四个恢复入口（进环 HITL 裁决）。计数点是「向图发起续跑」。
    record_graph_resume("hitl_in_loop")
    return await graph.ainvoke(
        Command(resume=resume_value),
        config=config,
        **durability_kwargs(),
    )


def _node_timeout_kwargs() -> Dict[str, Any]:
    """LG-3-2：主图节点的原生超时实参；**未启用时返回空 dict**。

    返回空 dict 而不是 ``{"timeout": None}`` 是本项的口径：开关为 0 时
    ``add_node`` 完全不传该形参，注册出来的节点与基线逐位等价（纪律 1）。
    策略对象只从 ``policies.node_timeout`` 出（LG-0-3 集中点，且
    ``tests/test_p147_policy_centralization.py`` 按实参来源判违规）。

    只对**异步节点**合法：超时依赖 asyncio 协作式取消，而同步节点无法在
    进程内被安全取消——框架在 ``compile().validate()`` 阶段就抛 ``ValueError``，
    不会留到运行期才发现（边界说明见指导文档 §4.4 LG-3-2 验收第 ③ 条）。
    """
    policy = node_timeout(settings.NODE_TIMEOUT_SECONDS)
    return {"timeout": policy} if policy is not None else {}


def _node_error_handler_kwargs() -> Dict[str, Any]:
    """LG-3-5：有副作用节点的原生错误补偿实参；**未启用时返回空 dict**。

    返回空 dict 而不是 ``{"error_handler": None}`` 是本项的口径：开关关闭时
    ``add_node`` 完全不传该形参，注册出来的图与基线逐位等价（纪律 1）。
    handler 本体由 ``policies.node_error_handler`` 构造（LG-0-3 集中点）。

    只挂 ``execute_task`` / ``code_agent``：它们是**有副作用**的节点，失败时
    整轮 500 等于把「哪些副作用已经发生」丢给调用方猜。补偿 handler 只写记录
    并收尾，**不重跑节点**，所以与 LG-1-1「这两个节点不挂 retry_policy」不冲突。
    """
    if not settings.NODE_ERROR_HANDLER_ENABLED:
        return {}
    # 补偿记录落库与 T18 共用同一装配判据（hitl_bridge.resolve_pending_store），
    # 不在此另判 flag；import 留在函数内，flag-off 时连 sqlalchemy 都不拉。
    from agentix.policies.hitl.hitl_bridge import resolve_pending_store

    return {"error_handler": node_error_handler(store=resolve_pending_store())}


def build_graph(checkpointer=None):
    """构建并编译主图；checkpointer 由启动期注入（Redis 实例或 None/MemorySaver）。

    P0-1：原模块级编译改为工厂函数，FastAPI 启动期会用 Redis checkpointer
    重编译并覆盖下方模块级 `graph`；checkpointer 为 None 时图不持久化。
    """
    builder = StateGraph(AgentState, input=InputState)
    # LG-1-1：只读节点的原生重试。flag-off ⇒ 下面是**空 dict**，add_node 完全不传
    # `retry_policy` ⇒ 与基线逐位等价（纪律 1）；策略实参来自 policies.py（LG-0-3 集中点），
    # 不在本文件内联构造。覆盖面只有 6 个只读节点，`execute_task` / `code_agent` 永不挂
    # （整节点重跑会重放工具副作用）。
    readonly_retry_kwargs = (
        {"retry_policy": readonly_retry_policy()} if settings.NODE_RETRY_ENABLED else {}
    )
    # LG-3-2：节点级原生超时（langgraph 1.x 的 `add_node(timeout=)`）。flag-off
    # （`NODE_TIMEOUT_SECONDS=0`，默认）⇒ 下面是**空 dict** ⇒ add_node 完全不传
    # `timeout` ⇒ 与基线逐位等价（纪律 1）。超时是**每次节点尝试**的墙钟上限，
    # 靠 asyncio 协作式取消生效（纪律 6 ②：不让出事件循环的节点杀不掉）；
    # `NodeTimeoutError` 已在领域分类里判为不可重试，不会与上面的 retry_policy
    # 叠成「超时→重试→再超时」（LG-3-2 验收第 ④ 条）。
    node_timeout_kwargs = _node_timeout_kwargs()
    # LG-3-5：有副作用节点的原生错误补偿（langgraph 1.x 的 `add_node(error_handler=)`）。
    # flag-off（默认）⇒ 空 dict ⇒ 完全不传该形参（纪律 1）；开时仅在
    # execute_task / code_agent 上挂 handler（见 _node_error_handler_kwargs 的边界说明）。
    node_error_handler_kwargs = _node_error_handler_kwargs()
    # 策略形参口径（LG-0-3）：下面这些 add_node 若要挂 retry_policy / cache_policy /
    # error_handler / timeout，实参**只能**来自 agentix/orchestration/langgraph/policies.py
    # 的工厂；在本文件内联 `RetryPolicy(...)` / `TimeoutPolicy(...)` 由
    # tests/test_p147_policy_centralization.py 判违规（散落的字面量改一处漏一处且不报错）。
    # 接线进度：LG-1-1 已给 6 个只读节点挂 retry_policy（见上方 readonly_retry_kwargs）；
    # LG-3-2 已给全部 10 个节点挂 timeout（见 node_timeout_kwargs）；
    # LG-3-5 已给 2 个有副作用节点挂 error_handler（见 node_error_handler_kwargs）；
    # cache_policy 仍未接线（不在本轮范围）。三个 kwargs 都用 `**` 展开，
    # 关时完全不传形参——守卫（p147 判据 2）对 `**` 展开显式豁免的正是这一形态。
    # 注意：check_hallucinations 故意不在此注册（本块仅注册下列 10 个节点：
    # message_window / analyze_and_route_query / respond_to_general_query /
    # get_additional_info / create_research_plan / create_image_query /
    # create_file_query / create_kb_query / execute_task / code_agent），
    # 其定义见本模块上方 check_hallucinations(...)，标注为 DEAD-CODE(P2-5)｜保留·有意不接线。
    # 批次 7：入口插入历史窗口化节点（settings.MAX_HISTORY_MESSAGES<=0 时
    # 为 no-op，行为与旧版一致），裁剪 checkpointer 持久化的无界历史。
    # 上下文工程 C：LG_TOKEN_HISTORY_BUDGET>0 时在条数窗口内再加 token 预算
    # （默认 0 ⇒ 该分支不执行，与基线逐位等价）。
    builder.add_node(
        "message_window",
        create_message_window_node(
            settings.MAX_HISTORY_MESSAGES,
            getattr(settings, "LG_TOKEN_HISTORY_BUDGET", 0),
        ),
        **node_timeout_kwargs,
    )
    builder.add_node(analyze_and_route_query, **readonly_retry_kwargs, **node_timeout_kwargs)
    builder.add_node(respond_to_general_query, **readonly_retry_kwargs, **node_timeout_kwargs)
    builder.add_node(get_additional_info, **node_timeout_kwargs)
    builder.add_node(
        "create_research_plan",
        create_research_plan,
        **readonly_retry_kwargs,
        **node_timeout_kwargs,
    )
    builder.add_node(create_image_query, **readonly_retry_kwargs, **node_timeout_kwargs)
    builder.add_node(create_file_query, **readonly_retry_kwargs, **node_timeout_kwargs)
    builder.add_node(create_kb_query, **readonly_retry_kwargs, **node_timeout_kwargs)
    # P1（任务执行）：execute_task 无条件注册（route_query 的 Literal/ROUTE_HANDLERS
    # 需与节点集对齐，phase19 绊线断言两者相等）；可达性由 TASK_EXEC_ENABLED 在
    # 路由侧门控——flag-off 时任何路由决策都不会进入本节点，行为与基线等价。
    # F1e·A-P1-4：注册时包一层进环注入壳（节点名不变 = 绊线断言不受影响；
    # flag-off/3.10/未声明进环意图时纯透传）。
    builder.add_node(
        "execute_task",
        _wrap_execute_task_in_loop(execute_task),
        **node_error_handler_kwargs,
        **node_timeout_kwargs,
    )
    # P2（代码执行）：code_agent 无条件注册（route_query 的 Literal / ROUTE_HANDLERS
    # 需与节点集对齐，phase19 绊线断言两者相等）；可达性由 CODE_EXEC_ENABLED 在
    # 路由侧门控——flag-off 时任何路由决策都不会进入本节点，行为与基线等价。
    builder.add_node(
        "code_agent",
        create_code_query,
        **node_error_handler_kwargs,
        **node_timeout_kwargs,
    )

    builder.add_edge(START, "message_window")
    builder.add_edge("message_window", "analyze_and_route_query")
    builder.add_conditional_edges("analyze_and_route_query", route_query)
    return builder.compile(checkpointer=checkpointer)


# 向后兼容：模块级默认图（MemorySaver）。
# 独立 CLI 入口（application/agents/main.py）与单元测试依赖此符号；
# FastAPI 启动期若 Redis checkpointer 就绪，会覆盖 lg_builder.graph 为
# Redis 持久化版本——外部访问请用 `lg_builder.graph` 属性（动态取值），
# 不要 `from lg_builder import graph`（值绑定不会跟随启动期覆盖）。
checkpointer = MemorySaver()
graph = build_graph(checkpointer=checkpointer)

# ---------------------------------------------------------------------------
# Phase 19：multi_agent 接线（supervisor 拓扑切换）
# ---------------------------------------------------------------------------
#: supervisor 拓扑编译结果。惰性：仅 flag 开启（或启动期显式重建）时编译；
#: main.py 启动期在 Redis checkpointer 就绪后会同时重建 graph 与本图，
#: 保证两拓扑共享同一 checkpointer（thread_id=session_id 语义一致）。
supervisor_graph: Any = None


def build_supervisor_graph(checkpointer=None):
    """构建 supervisor 多智能体拓扑（Phase 19）。

    实现在 ``agentix.orchestration.langgraph.lg_supervisor``（原 application
    路径已随 M6 删除）；此处惰性导入，
    避免 lg_builder ↔ lg_supervisor 的模块级循环（lg_supervisor 顶层
    import 本模块的 handler / 窗口节点工厂）。
    """
    from agentix.orchestration.langgraph.lg_supervisor import (
        build_supervisor_graph as _build_supervisor_graph,
    )

    return _build_supervisor_graph(checkpointer=checkpointer)


def get_active_graph():
    """Phase 19 图选择：``AGENTIX_ABLATION_MULTI_AGENT=true`` ⇒ supervisor 拓扑。

    默认（未注入 / False）⇒ 扁平图 ``graph``——现网零变化（flag-off 等价）。
    经 ``chat_service._graph_getter`` 惰性调用：不缓存开关值，与
    ``ablation.is_enabled`` 每请求读取语义一致（消融子进程注入即生效）。
    """
    global supervisor_graph
    if ablation.is_enabled("multi_agent"):
        if supervisor_graph is None:
            supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
        return supervisor_graph
    return graph


async def safety_guardrails(state: AgentState, *, config: RunnableConfig) -> Dict[str, List[BaseMessage]]:
    """Backward-compatible wrapper for guardrails logic."""
    return await get_additional_info(state, config=config)
