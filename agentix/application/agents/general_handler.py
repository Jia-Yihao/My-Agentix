"""
General query handling module - default LLM responses and KB queries.
"""

from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableConfig
from loguru import logger

from agentix.orchestration.langgraph.lg_states import AgentState
from agentix.application.agents.lg_prompts import GENERAL_QUERY_SYSTEM_PROMPT, KB_QUERY_REWRITE_PROMPT
from agentix.application.agents.clarify import clarify_reply, should_clarify
from agentix.application.agents.llm_factory import get_llm_client
from agentix.application.agents.router import _ensure_router, _extract_configurable
from agentix.application.agents.kb_tools import create_knowledge_query_node, KnowledgeQueryInputState
from agentix.config import ablation, settings
from agentix.application.services.knowledge_gateway import KnowledgeGateway, get_retrieval_port


def _maybe_clarify(state: AgentState, question: str) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Phase 28（Ch9 §9.3.2）：澄清前置门（flag 关零成本 ⇒ 入口与基线字节等价）。

    返回 ``(澄清更新, 需解除上膛)``：

    - 更新非 ``None`` ⇒ 调用方直接 ``return 更新`` 即完成澄清
      （结构化 AIMessage + 候选项；不入 HITL、不计失败、不改路由终态），
      同时会话态 ``clarify_armed=True``（单轮上膛）；
    - ``需解除上膛=True`` ⇒ 本轮判定明确（无歧义），正常结果须合并
      ``clarify_armed=False`` 恢复可澄清；
    - 已上膛且仍歧义 ⇒ 强制路由（既不上膛也不解除——防澄清死循环，
      连续澄清至多 1 次，Ch5 禁再入循环）。
    """
    if not settings.CLARIFY_ROUTING_ENABLED:
        return None, False
    armed = bool(getattr(state, "clarify_armed", False))
    confidence = getattr(getattr(state, "router", None), "confidence", None)
    decision = should_clarify(question, confidence=confidence)
    if not decision.should:
        return None, armed
    if armed:
        return None, False
    return {"messages": [clarify_reply(decision)], "clarify_armed": True}, False


def _status_tail(state: AgentState, configurable: Dict[str, Any]) -> str:
    """上下文工程 D：状态尾注文本（flag 关或无内容 ⇒ 返回空串）。

    结构化元信息（轮次 / 历史量 / 预算余量）**只写入本轮 human 消息尾部**，
    绝不进入静态 system 前缀——动态量进前缀会打穿 KV Cache（N 个易变量 =
    2^N 个缓存变体）。数据来源全部可信（本机计数 + 预算台账快照），
    模型自身不写状态。
    """
    if not bool(getattr(settings, "LG_STATUS_TAIL_ENABLED", False)):
        return ""
    messages = list(getattr(state, "messages", None) or [])
    turns = sum(1 for m in messages if getattr(m, "type", "") == "ai") + 1
    parts = [f"对话第 {turns} 轮", f"历史消息 {len(messages)} 条"]
    budget = configurable.get("budget_status")
    if budget:
        parts.append(str(budget))
    return "[状态] " + " | ".join(parts)


async def respond_to_general_query(state: AgentState, *, config: RunnableConfig) -> Dict[str, List[BaseMessage]]:
    """Generate response for general queries using pure LLM, no external tools."""
    logger.info("-----generate general-query response-----")

    # Phase 28 澄清前置门：flag 关立即返回（字节等价）；flag 开且歧义 ⇒
    # 提前返回澄清应答（本函数体入口先于 LLM 客户端构造）。
    clarify_update, clear_armed = _maybe_clarify(
        state, state.messages[-1].content if state.messages else ""
    )
    if clarify_update is not None:
        return clarify_update

    # P0-2(b)：显式绑定 general 角色（temperature=0.7, timeout=45s, max_retries=2）。
    # 与默认 role="general" 行为一致，此处显式声明只为留痕、可观测（role_tag）。
    model = get_llm_client(temperature=0.7, tags=["general_query"], role="general")

    router = _ensure_router(
        getattr(state, "router", None),
        fallback_question=state.messages[-1].content if state.messages else "",
    )
    state.router = router
    system_prompt = GENERAL_QUERY_SYSTEM_PROMPT.format(logic=router.logic)

    messages = [{"role": "system", "content": system_prompt}]
    configurable = _extract_configurable(config) or {}
    # 上下文工程 B4：指令分层块（LG_INSTRUCTIONS_ENABLED，默认关）。
    # 紧跟静态 system 常量之后、memory_context 之前——「越稳定的前缀越靠前」：
    # 指令块来自约定文件（近似常驻），memory 随会话变化。
    # 与 memory 同为瞬态注入：只进本次 ainvoke，不写回 state.messages。
    instructions = configurable.get("instructions")
    if instructions:
        messages.append(
            {
                "role": "system",
                "content": (
                    "以下是本项目/会话的常驻协作约定（AGENTS.md 级联），"
                    "供回答时遵循；若与用户当前请求无关，不要主动展开。\n"
                    f"{instructions}"
                ),
            }
        )
    # 批次 7：长期记忆上下文（chat_service 经 configurable 传入；默认关）。
    # **瞬态注入**：只进本次 ainvoke 的消息列表，不写回 state.messages，
    # 因此不会被 add_messages 持久化、不污染 checkpointer 历史。
    memory_context = configurable.get("memory_context")
    if memory_context:
        messages.append(
            {
                "role": "system",
                "content": (
                    "以下是与该用户相关的历史记忆摘要，仅供理解上下文参考；"
                    "可能过时或不完整，回答时不得将其当作已确认的事实来源。\n"
                    f"{memory_context}"
                ),
            }
        )
    messages += state.messages

    # 上下文工程 D：状态尾注（LG_STATUS_TAIL_ENABLED，默认关）——把
    # 「轮次 / 历史量 / 预算余量」追加到**本轮 human 消息尾部**（Manus 式：
    # 动态元信息一律走尾部，静态前缀逐字节不变）。用 model_copy 生成副本，
    # 不改写 state.messages ⇒ checkpointer 历史零污染。
    status_tail = _status_tail(state, configurable)
    if status_tail and messages:
        last = messages[-1]
        content = getattr(last, "content", None)
        # 只追加到本轮 human 消息尾部；历史为空（last 是 system dict）时跳过。
        if getattr(last, "type", "") == "human" and isinstance(content, str):
            messages[-1] = last.model_copy(
                update={"content": f"{content}\n\n{status_tail}"}
            )
    # P1-4：转发 config，使本节点的 chat model 事件冒泡到 astream_events，
    # 从而产出逐 token 的真流式（见 chat_service.stream_events 白名单）。
    response = await model.ainvoke(messages, config=config)
    result: Dict[str, Any] = {"messages": [response]}
    if clear_armed:
        # Phase 28：上一轮已澄清、本轮判定明确 ⇒ 解除单轮约束（恢复可澄清）。
        result["clarify_armed"] = False
    return result


# Phase 24（Ch3 Agentic RAG）：空检索时的查询改写重试上限（总调用 ≤ 1+2 次，步数有界）。
_QUERY_REWRITE_MAX_ATTEMPTS = 2


def _record_rewrite_attempt(attempt: int, *, status: str, hits: int, error: str = "") -> None:
    """每次改写尝试进 trajectory（kind=retrieval, name=agentic_rag_rewrite）。
    懒 import + 全吞（抄 config_watch 范式）：观测失败绝不影响主链路。"""
    try:
        from agentix.application.services.trajectory import get_trajectory_recorder

        detail: Dict[str, Any] = {"attempt": attempt, "hits": hits, "reason": "empty_sources"}
        if error:
            detail["error"] = error
        get_trajectory_recorder().record(
            kind="retrieval",
            name="agentic_rag_rewrite",
            status=status,
            detail=detail,
        )
    except Exception:  # noqa: BLE001
        logger.warning("agentic_rag trajectory record failed (non-fatal)")


async def _retry_kb_query_with_rewrite(
    *,
    llm: Any,
    workflow: Any,
    question: str,
    history: List[Dict[str, Any]],
    response: Dict[str, Any],
    config: RunnableConfig,
) -> Dict[str, Any]:
    """Phase 24（Ch3 Agentic RAG）：空检索 → 改写重试 ≤2 → 仍空保留诚实文案返回。

    - 每次尝试：改写提示 → LLM 输出新查询（空输出退化为原查询防空转）→ 重跑 workflow；
    - 改写 LLM 或 workflow 抛异常 → 记录 error 后立即停止（降级不崩，保留最后一次响应）；
    - 循环有界：最多 _QUERY_REWRITE_MAX_ATTEMPTS 次改写（总 workflow 调用 ≤3）；
    - 仍为空时 workflow finalize 的既有诚实声明原样返回，不做任何伪造。
    """
    current_question = question
    for attempt in range(1, _QUERY_REWRITE_MAX_ATTEMPTS + 1):
        if response.get("sources"):
            break
        try:
            rewrite_prompt = KB_QUERY_REWRITE_PROMPT.format(question=current_question)
            rewritten = await llm.ainvoke([{"role": "user", "content": rewrite_prompt}])
            new_question = (getattr(rewritten, "content", "") or "").strip() or current_question
            response = await workflow.ainvoke(
                {"question": new_question, "history": history},
                config=config,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("agentic_rag rewrite attempt %s failed (%s); keeping last response.", attempt, exc)
            _record_rewrite_attempt(attempt, status="error", hits=len(response.get("sources") or []), error=str(exc))
            break
        current_question = new_question
        _record_rewrite_attempt(attempt, status="ok", hits=len(response.get("sources") or []))
    return response


async def create_kb_query(state: AgentState, *, config: RunnableConfig) -> Dict[str, List[BaseMessage]]:
    """Query the vector knowledge base via a multi-agent workflow."""
    logger.info("------execute KB multi-tool query------")

    last_message = state.messages[-1].content if state.messages else ""
    if not last_message.strip():
        return {"messages": [AIMessage(content="请告诉我具体的问题，我才能帮您查询知识库。")]}

    # Phase 28 澄清前置门（与 respond_to_general_query 同点；消融支路委托
    # respond 时会再走一次门——判定是纯函数，双次评估结果一致，不会二次澄清）。
    clarify_update, clear_armed = _maybe_clarify(state, last_message)
    if clarify_update is not None:
        return clarify_update

    # 消融开关（AGENTIX_ABLATION_RETRIEVAL_ENABLED=false）：
    # 跳过 KB 多工具检索与图谱检索，直接走纯 LLM 作答——即变体 A_llm_only 的「裸 LLM 直答」。
    # 这里**不**打 degraded 标记：这是实验设定下的有意行为，不是系统故障；
    # 打标会污染评测里 degraded 这一类计数。
    if not ablation.is_enabled("retrieval_enabled"):
        logger.info(
            "ablation[retrieval_enabled=False]: skip retrieval, answer with LLM directly"
        )
        return await respond_to_general_query(state, config=config)

    config_opts = _extract_configurable(config)
    kb_top_k = config_opts.get("kb_top_k") or settings.KB_TOP_K
    kb_similarity_threshold = (
        config_opts.get("kb_similarity_threshold")
        if config_opts.get("kb_similarity_threshold") is not None
        else settings.KB_SIMILARITY_THRESHOLD
    )
    kb_filter_expr = config_opts.get("kb_filter_expr")

    knowledge_service: Optional[KnowledgeGateway] = None
    try:
        # P0-2(b) 未接线（后续项）：同一 llm 实例同时服务 KB 子图内多角色
        # （router / guardrails / planner / tool_selection / finalize），
        # 需 per-node 注入才能差异化，已列为后续项。
        llm = get_llm_client(temperature=0.3, tags=["kb_multi_tool"])

        knowledge_service = KnowledgeGateway()

        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.multi_tool import (
            create_kb_multi_tool_workflow,
        )

        external_url = settings.KB_EXTERNAL_SEARCH_URL
        if not external_url and settings.INGEST_SERVICE_URL:
            external_url = f"{settings.INGEST_SERVICE_URL.rstrip('/')}/api/search"

        workflow = create_kb_multi_tool_workflow(
            llm=llm,
            knowledge_service=knowledge_service,
            top_k=kb_top_k,
            similarity_threshold=kb_similarity_threshold,
            filter_expr=kb_filter_expr,
            allow_external=settings.KB_ENABLE_EXTERNAL_SEARCH,
            external_search_url=external_url,
        )

        history_payload = [
            {
                "role": getattr(msg, "type", "user"),
                "content": getattr(msg, "content", ""),
            }
            for msg in state.messages[:-1]
            if getattr(msg, "content", "").strip()
        ]

        # P1-4：把 config 透传进 KB 子图，子图 finalize 节点再转发给 LLM，
        # 形成「主图 create_kb_query → 子图 finalize → chat model」的事件链。
        response = await workflow.ainvoke(
            {
                "question": last_message,
                "history": history_payload,
            },
            config=config,
        )

        # Phase 24（Ch3 Agentic RAG，AGENTIC_RAG_ENABLED 默认关）：
        # 空检索 → 查询改写重试 ≤2 次；仍空保留 finalize 诚实文案。
        # flag-off 时此条件为 False ⇒ 与基线逐字节等价（零额外 invoke、零轨迹）。
        if settings.AGENTIC_RAG_ENABLED and not response.get("sources"):
            response = await _retry_kb_query_with_rewrite(
                llm=llm,
                workflow=workflow,
                question=last_message,
                history=history_payload,
                response=response,
                config=config,
            )

        answer_text = response.get("answer") or "检索完成，但暂时没有可以分享的结果。"
        sources = response.get("sources", [])

        ai_message = AIMessage(content=answer_text)
        ai_message.additional_kwargs["sources"] = sources
        # P1-5：KB 子图内某检索通道失败（部分成功）时标记 degraded，
        # 与下方 fallback 分支同一机制，供 chat 层返回 status="degraded"。
        if response.get("degraded"):
            ai_message.additional_kwargs["degraded"] = True

        kb_result: Dict[str, Any] = {"messages": [ai_message], "sources": sources}
        if clear_armed:
            kb_result["clarify_armed"] = False
        return kb_result
    except Exception as exc:
        logger.warning("KB multi-tool workflow unavailable (%s); falling back to direct search.", exc)

    # Fallback: direct KB query
    # P0-3：多工具工作流失败属于系统故障（替代路径降级），最终答复必须
    # 携带 degraded 标记——chat 层据此返回 status="degraded"，
    # 让前端区分「系统降级」与「知识库无记载」，避免降级文案被当成法律答复。
    if knowledge_service is None:
        knowledge_service = KnowledgeGateway()
    knowledge_node = create_knowledge_query_node(retrieval_port=get_retrieval_port())
    input_state: KnowledgeQueryInputState = {
        "task": last_message,
        "context": {
            "top_k": kb_top_k,
            "similarity_threshold": kb_similarity_threshold,
            "filter_expr": kb_filter_expr,
        },
        "steps": ["kb_query"],
    }
    result = await knowledge_node(input_state)
    answer_text = result.get("answer", "") or "抱歉，我暂时无法从知识库中找到答案。"
    ai_message = AIMessage(content=answer_text)
    ai_message.additional_kwargs["degraded"] = True  # P0-3：降级标记
    fb_result: Dict[str, Any] = {"messages": [ai_message]}
    if clear_armed:
        fb_result["clarify_armed"] = False
    return fb_result
