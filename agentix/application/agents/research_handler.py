"""KG 多工具子图节点真身（M3-4，§7.5 第③步的续批：把最后一个住在编排层的节点实现迁进 application）。

## 这个文件为什么存在

`create_research_plan` 是主图 10 个节点里**唯一真身仍住在 `orchestration`** 的那个：它要用的
`get_neo4j_graph` / `LegalCypherRetriever` / `create_multi_tool_workflow` / `get_llm_client` /
`SCOPE_DESCRIPTION` / `predefined_cypher_dict` 全是 application 侧的具体实现，真身留在
`agentix/orchestration/langgraph/lg_builder.py`，`orchestration → application` 这条反向边
就一直拆不干净（§7.5 点名的 M3 目标）。把真身连同它**独占**的两个私有辅助函数
（`_extract_legal_sources`、`_get_compiled_multi_tool_workflow`）一起迁进来，那几条 KG 面
import 随函数一起离开编排层——本模块因此成为 KG 面具体实现的唯一落点。

节点在图上仍由 `lg_builder` 的**同名延迟解析壳**承载：本模块不参与建图。理由与其余 9 个
节点完全一致——`lg_builder` 在 **import 期**就执行 `graph = build_graph(...)`，而 `evals/`、
`bench/`、独立脚本拉起这张图时都不加载装配根，解析只能发生在**调用期**（机制见
`agentix/orchestration/langgraph/lg_builder.py` 的「延迟解析壳」区注释与
`agentix/orchestration/node_registry.py` 的模块 docstring）。

- 注册键：`create_research_plan`（与壳名逐字相同）
- 装配落点：`agentix/application/wiring.py`
- 登记形态：`awaits=True, takes_config=True`（迁移前后一个字未改）

## 消融分支为什么经注册表、而不是直接同层调用

`ablation.tools_enabled=False` 的那条退回分支刻意**不**写成同层直接调用，而是
`node_registry.resolve("respond_to_general_query")`：M3-3 的既有不变式是「这条分支同样经注册表
解析、装配缺失时响亮抛 `NodeNotRegistered`」。直接同层调用会让这条分支**不经装配也能跑**，
等于静默改掉一个已审过的失效面（fail-closed 被绕开且无人察觉）。

`AgentState` 仍从 `agentix.orchestration.langgraph.lg_states` 取：`application → orchestration`
在 M1 允许集的基础方向里（`tests/test_p177_arch_closure.py` 的 `ALLOWED["application"]`），
本模块不新增任何允许集条目。
"""

from functools import lru_cache
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from loguru import logger

from agentix.config import ablation, settings
from agentix.orchestration import node_registry
from agentix.orchestration.langgraph.lg_states import AgentState
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
from agentix.tools.registry_init import get_active_tools


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
        return await node_registry.resolve("respond_to_general_query")(state, config=config)

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
