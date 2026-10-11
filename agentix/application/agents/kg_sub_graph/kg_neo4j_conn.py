"""Neo4j 连接管理（P1-2a：进程内单例 + R-P0-4 schema TTL 刷新）。

原实现每次调用 ``get_neo4j_graph()`` 都新建 ``Neo4jGraph``——而
``Neo4jGraph`` 默认 ``refresh_schema=True``，**每次实例化都会扫全库
schema**，在每请求新建的场景下这是隐性巨坑（方案 P1-2）。

改为进程内单例：
- 双检锁（``threading.Lock``）避免并发重复建连；保持**同步签名**，
  现有同步调用点（guardrails / cypher_tools / text2sql_tool /
  main / lg_builder）零改动。
- ``refresh_schema=False`` 构造（不扫全库），仅在首次建连时手动
  ``refresh_schema()`` 一次——同时充当连通性健康检查（连不上直接抛）。
- 底层 neo4j Driver 自带连接池与断线自动重连，单例持有安全；仅当
  首次建连失败时抛异常（不缓存失败状态），下次调用自动重试。
- R-P0-4：单例不再永久持旧 schema——``NEO4J_SCHEMA_REFRESH_SECONDS > 0``
  时（默认 300s），下次取用在锁内再 ``refresh_schema()``；刷新失败保留
  旧 schema + warning（不打断请求，按 TTL 周期重试）；``<=0`` 回退首连
  刷一次的旧行为。

另提供 R-P0-4 的 Cypher 执行缝公共件（``query_readonly``）：
- ``find_write_clauses`` / ``assert_readonly_cypher``：基于
  ``constants.WRITE_CLAUSES`` 单一黑名单的执行期只读闸；
- ``query_readonly``：只读闸前置 + 可选执行超时（
  ``NEO4J_QUERY_TIMEOUT_SECONDS``，默认 0 = 与基线逐位等价）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from typing import Any, Dict, Optional

from langchain_neo4j import Neo4jGraph

from agentix.config import settings
from agentix.infrastructure.core.logger import get_logger

# 获取日志记录器
logger = get_logger(service="kg_builder")

# 设置Neo4j驱动的日志级别为ERROR，禁止WARNING消息
logging.getLogger("neo4j").setLevel(logging.ERROR)
# 禁用langchain_neo4j相关日志
logging.getLogger("langchain_neo4j").setLevel(logging.ERROR)
# 禁用驱动相关日志
logging.getLogger("neo4j.io").setLevel(logging.ERROR)
logging.getLogger("neo4j.bolt").setLevel(logging.ERROR)

# 进程内单例缓存与建连锁（P1-2a）
_graph_cache: Optional[Neo4jGraph] = None
_graph_lock = threading.Lock()
#: 首建/最近一次 schema 刷新的 monotonic 时刻（R-P0-4 TTL 刷新）
_schema_refreshed_at: Optional[float] = None


def _build_graph() -> Neo4jGraph:
    """按配置新建 Neo4jGraph（refresh_schema=False，构造期不扫全库 schema）。"""
    kwargs = {
        "url": settings.NEO4J_URI,
        "database": settings.NEO4J_DATABASE,
        # 性能关键点：默认每次实例化都会扫全库 schema（方案 P1-2 隐性巨坑）；
        # 单例模式下只需首建后 refresh_schema() 一次。
        "refresh_schema": False,
    }
    if settings.NEO4J_USER and settings.NEO4J_PASSWORD not in (None, ""):
        kwargs.update({
            "username": settings.NEO4J_USER,
            "password": settings.NEO4J_PASSWORD,
        })
    return Neo4jGraph(**kwargs)


def _schema_refresh_interval() -> float:
    """当前 TTL 刷新间隔秒数（非法/未配置回退 0 = 关闭周期刷新）。"""
    try:
        return float(getattr(settings, "NEO4J_SCHEMA_REFRESH_SECONDS", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _schema_refresh_due() -> bool:
    """是否已到周期刷新时刻（需要拿锁后重检——快路径仅无锁读）。"""
    ttl = _schema_refresh_interval()
    if ttl <= 0 or _graph_cache is None or _schema_refreshed_at is None:
        return False
    return (time.monotonic() - _schema_refreshed_at) >= ttl


def get_neo4j_graph() -> Neo4jGraph:
    """返回进程内唯一的 Neo4jGraph 实例（P1-2a 单例 + R-P0-4 TTL 刷新）。

    - 首次调用：建连 + ``refresh_schema()`` 一次（兼做连通性检查，
      连不上抛异常，不缓存失败状态）
    - 后续调用：TTL 未到直接返回缓存；``NEO4J_SCHEMA_REFRESH_SECONDS > 0``
      且已到期时在锁内刷新 schema（失败保留旧 schema + warning）
    """
    global _graph_cache, _schema_refreshed_at
    # 快路径：无锁读（CPython 对象引用读写原子）
    if _graph_cache is not None and not _schema_refresh_due():
        return _graph_cache
    with _graph_lock:
        if _graph_cache is None:
            logger.info(f"initialize Neo4j connection: {settings.NEO4J_URI}")
            graph = _build_graph()
            # 首建刷一次 schema：填充 graph.schema，同时充当连通性健康检查
            # （首建失败照常抛出且不缓存失败状态——本处不吞异常）
            graph.refresh_schema()
            _graph_cache = graph
            _schema_refreshed_at = time.monotonic()
        elif _schema_refresh_due():
            # R-P0-4 周期刷新：失败保留旧 schema（进程不挂，按 TTL 重试）；
            # 无论成败都推进时间戳，避免每次取用都重试打成风暴
            try:
                _graph_cache.refresh_schema()
            except Exception as exc:  # noqa: BLE001 - 刷新失败绝不能打挂请求
                logger.warning(f"Neo4j schema refresh failed (old schema kept): {exc}")
            finally:
                _schema_refreshed_at = time.monotonic()
    return _graph_cache


def reset_neo4j_graph() -> None:
    """清空单例缓存与刷新时间戳，下次 ``get_neo4j_graph()`` 将重新建连。

    用途：测试隔离、Neo4j 连接配置热更新后强制重建。
    """
    global _graph_cache, _schema_refreshed_at
    with _graph_lock:
        _graph_cache = None
        _schema_refreshed_at = None


# ---------------------------------------------------------------------------
# R-P0-4：Cypher 执行缝公共件（只读闸 + 可选执行超时）
# ---------------------------------------------------------------------------


def find_write_clauses(cypher_statement: str) -> list:
    """返回语句命中的写子句（黑名单单一事实来源 = ``constants.WRITE_CLAUSES``）。

    子串匹配语义与既有校验点
    （``text2cypher/validation/validators.py`` / ``cypher_tools/utils.py`` /
    ``text2cypher/execution/node.py``）完全一致——保守但安全（含
    ``CALL`` 子查询/存储过程与 ``APOC.`` 动态写路径）。
    """
    from agentix.application.agents.kg_sub_graph.agentic_rag_agents.constants import (
        WRITE_CLAUSES,
    )

    stmt = (cypher_statement or "").upper()
    return [wc for wc in sorted(WRITE_CLAUSES) if wc in stmt]


def assert_readonly_cypher(cypher_statement: str) -> None:
    """执行期只读闸：命中写子句直接 ``ValueError``（fail-closed，不触库）。"""
    hits = find_write_clauses(cypher_statement)
    if hits:
        raise ValueError(
            f"Cypher contains write clause(s): {', '.join(hits)} "
            f"(R-P0-4 read-only gate): {(cypher_statement or '')[:160]}"
        )


def _query_timeout_seconds() -> float:
    try:
        return float(getattr(settings, "NEO4J_QUERY_TIMEOUT_SECONDS", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Phase H·W2：Cypher 成本护栏（NEO4J_COST_GUARD_ENABLED，默认关 = 零变化）
# ---------------------------------------------------------------------------

_COST_RETURN_STAR_RE = re.compile(r"\bRETURN\s+\*", re.IGNORECASE)
_COST_LIMIT_RE = re.compile(r"\bLIMIT\b", re.IGNORECASE)
_COST_RETURN_RE = re.compile(r"\bRETURN\b", re.IGNORECASE)
_COST_OPTIONAL_MATCH_RE = re.compile(r"\bOPTIONAL\s+MATCH\b", re.IGNORECASE)
# 无界变长：*.. / *2..（上界开；*1..3、*..3 有界 ⇒ (?!\d) 放行）
_COST_UNBOUNDED_VARLEN_RE = re.compile(r"\*\s*\d*\s*\.\.(?!\d)")
# 关系模式/列表字面：[..*..]——含 * 且无有界上界（..n）⇒ 无界（保守子串语义）
_COST_REL_BRACKET_RE = re.compile(r"\[[^\[\]]*\]")
_COST_BOUNDED_UPPER_RE = re.compile(r"\.\.\s*\d+")


class GraphCostEstimator:
    """Cypher 成本预检（纯字符串，不触库；W2）。

    - **拒绝**（verdict=``rejected``，用例层 fail-closed 抛 ValueError）：
      ``RETURN *``（无界投影）、无界变长遍历（``*..``/``*2..``/``[r*]``）、
      ``OPTIONAL MATCH`` 堆叠（> ``MAX_OPTIONAL_MATCH`` 层）；
    - **限流**（verdict=``limited``）：简单单 ``RETURN`` 尾且无 ``LIMIT`` ⇒
      追加 ``LIMIT DEFAULT_LIMIT``（含 UNION/多 RETURN/分号结尾的复杂语句
      不改写，保持原样）；
    - 其余 verdict=``ok``（原样放行）。
    """

    MAX_OPTIONAL_MATCH = 4
    DEFAULT_LIMIT = 1000

    @classmethod
    def estimate(cls, cypher_statement: str) -> Dict[str, Any]:
        stmt = (cypher_statement or "").strip()
        if _COST_RETURN_STAR_RE.search(stmt):
            return {"verdict": "rejected", "reason": "return_star", "cypher": stmt}
        if _COST_UNBOUNDED_VARLEN_RE.search(stmt):
            return {
                "verdict": "rejected",
                "reason": "unbounded_var_length",
                "cypher": stmt,
            }
        for _m in _COST_REL_BRACKET_RE.finditer(stmt):
            _inner = _m.group(0)
            if "*" in _inner and not _COST_BOUNDED_UPPER_RE.search(_inner):
                return {
                    "verdict": "rejected",
                    "reason": "unbounded_var_length",
                    "cypher": stmt,
                }
        if len(_COST_OPTIONAL_MATCH_RE.findall(stmt)) > cls.MAX_OPTIONAL_MATCH:
            return {
                "verdict": "rejected",
                "reason": "optional_match_flood",
                "cypher": stmt,
            }
        upper = stmt.upper()
        simple_single_return = (
            len(_COST_RETURN_RE.findall(stmt)) == 1
            and "UNION" not in upper
            and not stmt.endswith(";")
        )
        if simple_single_return and not _COST_LIMIT_RE.search(stmt):
            return {
                "verdict": "limited",
                "reason": "limit_appended",
                "cypher": f"{stmt} LIMIT {cls.DEFAULT_LIMIT}",
            }
        return {"verdict": "ok", "reason": None, "cypher": stmt}


def query_readonly(
    graph: Neo4jGraph,
    cypher_statement: str,
    params: Optional[Dict[str, Any]] = None,
) -> Any:
    """Cypher 执行缝共用：只读前置闸 + 可选执行超时（R-P0-4）。

    - 先过只读闸：命中 ``WRITE_CLAUSES`` → ``ValueError``（语句绝不触库，
      异常沿节点既有失败分支传播）；
    - ``NEO4J_COST_GUARD_ENABLED=true``（W2，默认关）：再过 ``GraphCostEstimator``
      成本预检——``rejected`` 抛 ``ValueError``（不触库）；``limited`` 用追加
      ``LIMIT`` 的改写语句执行；flag-off 时语句逐位等价（零预检、零改写）；
    - ``NEO4J_QUERY_TIMEOUT_SECONDS <= 0``（默认）：同步内联 ``graph.query``
      （调用形状与各缝原直连逐位等价：无 params 走位置参数，有 params 走
      ``query=/params=`` 关键字）；
    - ``> 0``：返回 awaitable（``kg_gateway.run_governed`` 与
      ``ToolExecutor.execute`` 均会 await），经 ``to_thread`` + ``wait_for``
      限时执行；超时抛 ``RuntimeError``（后台线程无法中断，由其自行结束）。
    """
    assert_readonly_cypher(cypher_statement)
    stmt = cypher_statement
    if getattr(settings, "NEO4J_COST_GUARD_ENABLED", False):
        estimate = GraphCostEstimator.estimate(stmt)
        if estimate["verdict"] == "rejected":
            raise ValueError(
                f"Cypher rejected by cost guard ({estimate['reason']}): "
                f"{stmt[:160]}"
            )
        stmt = estimate["cypher"]
    timeout = _query_timeout_seconds()
    if timeout <= 0:
        if params is None:
            return graph.query(stmt)
        return graph.query(query=stmt, params=params)
    return _query_with_timeout(graph, stmt, params, timeout)


async def _query_with_timeout(
    graph: Neo4jGraph,
    cypher_statement: str,
    params: Optional[Dict[str, Any]],
    timeout: float,
) -> Any:
    def _run() -> Any:
        if params is None:
            return graph.query(cypher_statement)
        return graph.query(query=cypher_statement, params=params)

    try:
        return await asyncio.wait_for(asyncio.to_thread(_run), timeout)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"Neo4j query exceeded NEO4J_QUERY_TIMEOUT_SECONDS={timeout}: "
            f"{(cypher_statement or '')[:160]}"
        ) from exc
