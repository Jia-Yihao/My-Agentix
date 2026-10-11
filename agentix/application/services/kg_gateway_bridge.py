"""A2b 五项 KG/检索旁路收编装配（``TOOL_GATEWAY_ENFORCED``）。

职责分工（同 ``notify_bridge`` / ``task_tools_bridge`` 模式）：

- **声明**：四个 KG spec 在 ``registry_init`` 常驻清单，``search`` 在
  ``capabilities/tools/__init__``——本模块不动声明；
- **实现安装**：本模块只装 handler（``ensure_registered()`` +
  ``register_handler``），由 ``main.py::_startup`` 在 flag-on 时调用；
  flag-off 零接线（节点执行缝 ``kg_gateway.run_governed`` 直连基线）。

handler 即治理入口：执行经 ``ToolExecutor.execute`` ⇒ 有效风险/权限/HITL/
超时/重试/轨迹全部在前，本模块不提供任何旁路。graph/lightrag/工作流一律
**handler 内懒加载**——flag-off 时本模块零 import 成本。
"""

from __future__ import annotations

from agentix.config.settings import settings

__all__ = ["install_kg_gateway_handlers"]


def _neo4j_graph():
    """懒取 Neo4j graph（取不到/加载失败 → 抛错 → executor 落 failed）。"""
    try:
        from agentix.application.agents.kg_sub_graph.kg_neo4j_conn import (
            get_neo4j_graph,
        )
    except Exception as exc:  # pragma: no cover - defensive
        raise RuntimeError(f"Neo4j graph unavailable: {exc}") from exc
    graph = get_neo4j_graph()
    if graph is None:
        raise RuntimeError("Neo4j graph unavailable (get_neo4j_graph returned None)")
    return graph


def install_kg_gateway_handlers() -> None:
    """装齐五项收编 handler（幂等；flag-off 直接返回，零接线）。"""
    if not bool(getattr(settings, "TOOL_GATEWAY_ENFORCED", False)):
        return  # belt-and-suspenders：调用方已 gate，防御直调

    from agentix.tools.gateway.executor import get_tool_executor
    from agentix.tools.registry_init import ensure_registered

    ensure_registered()  # spec 先入 registry（executor.execute 的前置）

    executor = get_tool_executor()

    if not executor.has_handler("search"):

        def _search(args: dict):
            from agentix.tools.adapters.search import SearchTool

            return SearchTool().search(
                str(args.get("query") or ""),
                num_results=args.get("num_results"),
            )

        executor.register_handler("search", _search)

    if not executor.has_handler("cypher_query"):

        def _cypher_query(args: dict):
            # R-P0-4：执行缝只读闸 + 可选超时（handler 内懒加载，沿本模块风格）
            from agentix.application.agents.kg_sub_graph.kg_neo4j_conn import (
                query_readonly,
            )

            return query_readonly(_neo4j_graph(), str(args.get("statement") or ""))

        executor.register_handler("cypher_query", _cypher_query)

    if not executor.has_handler("predefined_cypher"):

        def _predefined_cypher(args: dict):
            from agentix.application.agents.kg_sub_graph.kg_neo4j_conn import (
                query_readonly,
            )

            return query_readonly(
                _neo4j_graph(),
                str(args.get("statement") or ""),
                args.get("params") or {},
            )

        executor.register_handler("predefined_cypher", _predefined_cypher)

    if not executor.has_handler("microsoft_graphrag_query"):

        async def _graphrag_query(args: dict):
            from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.customer_tools.node import (
                LightRAGAPI,
            )

            return await LightRAGAPI().query(str(args.get("query") or ""))

        executor.register_handler("microsoft_graphrag_query", _graphrag_query)

    if not executor.has_handler("text2sql_query"):

        async def _text2sql(args: dict):
            from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2cypher.text2sql_tool import (
                build_text2sql_workflow,
            )

            workflow = build_text2sql_workflow(
                graph=_neo4j_graph(),
                db_type=str(args.get("db_type") or "MySQL"),
                connection_string=args.get("connection_string"),
                max_retries=int(args.get("max_retries") or 3),
            )
            return await workflow.ainvoke(dict(args.get("input_state") or {}))

        executor.register_handler("text2sql_query", _text2sql)
