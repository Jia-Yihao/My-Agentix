"""schema 缓存的统一失效入口（`docs/design/性能与优化路线图.md` E2）。

仓里两套 schema 缓存各自长在不同模块：

- **text2sql**：``…/text2sql/schema_retrieval/node.py`` 的 TTL 字典
  （``TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS``，出厂 0 = 不缓存）；
- **Neo4j**：``kg_neo4j_conn`` 的单例 + 周期刷新
  （``NEO4J_SCHEMA_REFRESH_SECONDS``）。

E2 原文要求"失效入口同时服务两套缓存"，所以调用方（HTTP 运维面、导入脚本、以后的
schema 变更事件）只该看到这一个函数，不该知道两套缓存的清理语义不同——**一个是删条目，
一个是打标记**。把它们压成同一个返回值形态，是这里唯一的价值，也是这里唯一的风险：
谁以后加第三套缓存时必须继续维持这个形态，否则报告里的字段含义会开始漂移。

纪律 2（辅助子系统 fail-open）在这里的具体形态：任一侧抛错都只进 ``errors`` 字典，
本函数**不向上抛**，另一侧照常执行。清缓存失败的正确结局是"这次没清到、下次按 TTL 收敛"，
而不是把调用方（往往是刚跑完 DDL 的脚本）一起拖失败。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from agentix.infrastructure.core.logger import get_logger

logger = get_logger(service="schema_cache")


def invalidate_schema_caches(database_url: Optional[str] = None) -> Dict[str, Any]:
    """清 text2sql schema 缓存 + 标记 Neo4j schema 过期，返回一份可读的报告。

    参数：``database_url`` 只作用于 text2sql 那一侧（它的缓存键以目标库连接串开头），
    省略 = 清该进程里所有目标库的条目。Neo4j 侧没有"按库"的概念，恒为整份标记过期。

    报告字段（写进运维文档时要与这里逐字一致）：

    - ``text2sql_schema_cleared``：真清掉的条数；``None`` = 这一侧失败
    - ``text2sql_schema_cache_remaining``：清完之后这一侧还剩几条（``None`` = 这一侧失败）。
      只有 ``cleared`` 是不够的：``cleared=0`` 既可能是"本来就没缓存"，也可能是"一条都没清掉"，
      这两个形态对运维是完全不同的结论，多一个数就能分开
    - ``neo4j_schema_marked_stale``：``True`` = 已标记（下一次取用重刷）；
      ``False`` = 当前无实例；``None`` = 这一侧失败
    - ``errors``：``{侧名: "异常类型: 消息"}``，空字典 = 两侧都执行到位
    """
    report: Dict[str, Any] = {"errors": {}}

    try:
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2sql import (
            schema_retrieval,
        )

        report["text2sql_schema_cleared"] = schema_retrieval.invalidate_schema_cache(
            database_url
        )
        report["text2sql_schema_cache_remaining"] = schema_retrieval.schema_cache_size()
    except Exception as exc:  # noqa: BLE001 - 辅助设施：一侧失败不影响另一侧与调用方
        logger.warning(
            "text2sql schema 缓存失效失败（非致命，TTL 会自行收敛）: {}", exc
        )
        report["text2sql_schema_cleared"] = None
        report["text2sql_schema_cache_remaining"] = None
        report["errors"]["text2sql"] = f"{type(exc).__name__}: {exc}"

    try:
        from agentix.application.agents.kg_sub_graph import kg_neo4j_conn

        # 走模块属性调用（不是 `from … import 函数`）：测试与热补丁都钉在这一层。
        report["neo4j_schema_marked_stale"] = kg_neo4j_conn.invalidate_neo4j_schema()
    except Exception as exc:  # noqa: BLE001 - 同上：Neo4j 侧失败也不外抛
        logger.warning("Neo4j schema 失效标记失败（非致命）: {}", exc)
        report["neo4j_schema_marked_stale"] = None
        report["errors"]["neo4j"] = f"{type(exc).__name__}: {exc}"

    return report
