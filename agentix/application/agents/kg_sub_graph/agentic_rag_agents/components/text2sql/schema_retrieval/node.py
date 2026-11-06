"""
Schema retrieval node.

Fetches tables, columns, and relationships directly from MySQL INFORMATION_SCHEMA.
简化实现，不再依赖 Neo4j，直接从 MySQL 数据库读取表结构。
"""

import re
import time
from typing import Any, Callable, Coroutine, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from agentix.config import settings
from agentix.infrastructure.core.logger import get_logger
from ..domain_knowledge import (
    COLUMN_DESCRIPTIONS,
    RELATIONSHIP_FACTS,
    TABLE_DESCRIPTIONS,
)
from ..target_db import text2sql_database_url

logger = get_logger(service="text2sql.schema_retrieval")

_STOP_WORDS: Set[str] = {
    "the",
    "and",
    "for",
    "from",
    "with",
    "into",
    "what",
    "which",
    "when",
    "who",
    "where",
    "how",
    "many",
    "much",
    "that",
    "this",
    "those",
    "these",
    "all",
    "any",
    "year",
    "month",
    "day",
    "query",
    "please",
    "show",
    "list",
    "give",
    "查询",
    "一下",
    "所有",
    "哪些",
    "什么",
    "数据",
}


# R-P2-4（F2b-c）schema 缓存：TTL 由 TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS 控制
# （默认 0 = 关闭 = 基线逐位等价）；键 = (Text2SQL 目标库连接串, 归一问题)——schema_context
# 含按问题关键词筛表的结果，故不同问题不同缓存项。失败结果不入缓存。
_SCHEMA_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_SCHEMA_CACHE_MAX_ENTRIES = 128


def _schema_cache_key(question: str) -> str:
    return f"{text2sql_database_url()}|{(question or '').strip().lower()}"


def _schema_cache_put(key: str, value: Dict[str, Any], ttl: int) -> None:
    if len(_SCHEMA_CACHE) >= _SCHEMA_CACHE_MAX_ENTRIES:
        now = time.time()
        expired = [k for k, (exp, _) in _SCHEMA_CACHE.items() if exp <= now]
        for k in expired:
            _SCHEMA_CACHE.pop(k, None)
        while len(_SCHEMA_CACHE) >= _SCHEMA_CACHE_MAX_ENTRIES:
            _SCHEMA_CACHE.pop(next(iter(_SCHEMA_CACHE)))
    _SCHEMA_CACHE[key] = (time.time() + ttl, value)


def schema_cache_size() -> int:
    """当前缓存条目数（诊断与守卫用）。

    注意这是"**存着几条**"而不是"有几条还有效"：过期项靠取用时判 `exp > now`，
    所以这个数只用于核对"失效入口到底清掉了多少"，不要拿它当命中率分母。
    """
    return len(_SCHEMA_CACHE)


def invalidate_schema_cache(database_url: Optional[str] = None) -> int:
    """主动清 schema 缓存，返回**真清掉的条数**（E2）。

    为什么需要它：TTL 只保证"最多旧 N 秒"。DDL 之后运维要立刻看见新表结构，
    今天只有两条路——等 TTL，或重启进程（而重启会把别的缓存一并清掉）。
    返回值是刻意的：失效要能被核对成"我确实清到了 N 条"，而不是一个静默 void；
    清不到返回 0 也照实说，别把"没命中"报成"清好了"。

    为什么按库收窄（`database_url`）：缓存键以目标库连接串开头（见 `_schema_cache_key`），
    无条件全清会让一次调用打掉所有目标库的热缓存——那是一条"反复调用来放大延迟"的路径，
    所以默认口径是"你改了哪个库就清哪个库"，`None`（清全局）只留给整套库都变了的场景。
    """
    prefix = None if database_url is None else f"{database_url}|"
    targets = [
        key
        for key in list(_SCHEMA_CACHE)
        if prefix is None or key.startswith(prefix)
    ]
    for key in targets:
        _SCHEMA_CACHE.pop(key, None)
    return len(targets)


def create_schema_retrieval_node(
    neo4j_graph=None,  # 保留参数以兼容旧代码，但不再使用
) -> Callable[[Dict[str, Any]], Coroutine[Any, Any, Dict[str, Any]]]:
    """
    Build a LangGraph node that enriches the workflow state with schema
    metadata from MySQL INFORMATION_SCHEMA.
    """

    async def retrieve_schema(state: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("-----开始检索数据库 Schema (直接从 MySQL)-----")

        question = (state.get("question") or "").strip()

        ttl = int(getattr(settings, "TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS", 0) or 0)
        cache_key = None
        if ttl > 0:
            cache_key = _schema_cache_key(question)
            hit = _SCHEMA_CACHE.get(cache_key)
            if hit and hit[0] > time.time():
                logger.info("Schema 缓存命中（TTL={}s）", ttl)
                try:
                    from agentix.observability.metrics import record_cache_hit

                    record_cache_hit("text2sql_schema")
                except Exception:  # noqa: BLE001 - 埋点绝不影响主链路
                    logger.debug("schema cache hit metric failed (non-fatal)")
                return {
                    "schema_context": hit[1],
                    "value_mappings": {},
                    "mappings_str": "",
                    "steps": ["schema_retrieval"],
                }
            try:
                from agentix.observability.metrics import record_cache_miss

                record_cache_miss("text2sql_schema")
            except Exception:  # noqa: BLE001
                logger.debug("schema cache miss metric failed (non-fatal)")

        try:
            schema_context = await _retrieve_schema_from_mysql(question)

            logger.info(
                "Schema 检索完成，匹配到 {} 张表", len(schema_context.get("tables", []))
            )

            if cache_key is not None and ttl > 0:
                _schema_cache_put(cache_key, schema_context, ttl)

            return {
                "schema_context": schema_context,
                "value_mappings": {},  # 简化实现，不再使用值映射
                "mappings_str": "",
                "steps": ["schema_retrieval"],
            }
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("Schema 检索失败: {}", exc)
            return {
                "schema_context": {},
                "value_mappings": {},
                "mappings_str": "",
                "steps": ["schema_retrieval_failed"],
            }

    return retrieve_schema


def _extract_keywords(question: str) -> List[str]:
    if not question:
        return []

    tokens = re.findall(r"[a-zA-Z0-9_]+", question.lower())
    return [token for token in tokens if token and token not in _STOP_WORDS]


def _score_table(table: Dict[str, Any], keywords: Iterable[str]) -> float:
    if not keywords:
        return 0.0

    score = 0.0
    name = (table.get("table_name") or "").lower()
    description = (table.get("description") or "").lower()

    for kw in keywords:
        if kw in name:
            score += 2.0
        if kw in description:
            score += 1.0

    for column in table.get("columns", []):
        column_name = (column.get("column_name") or "").lower()
        column_desc = (column.get("description") or "").lower()
        for kw in keywords:
            if kw in column_name:
                score += 1.5
            if kw in column_desc:
                score += 0.75

    return score


async def _retrieve_schema_from_mysql(question: str) -> Dict[str, Any]:
    """
    直接从 MySQL INFORMATION_SCHEMA 读取表结构信息。
    """
    import asyncio

    def _sync_retrieve():
        engine: Engine = create_engine(text2sql_database_url(), future=True)
        try:
            with engine.connect() as conn:
                # 获取所有表信息
                tables_query = text("""
                    SELECT
                        TABLE_NAME,
                        TABLE_COMMENT
                    FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_SCHEMA = DATABASE()
                    AND TABLE_TYPE = 'BASE TABLE'
                    ORDER BY TABLE_NAME
                """)
                table_rows = conn.execute(tables_query).fetchall()

                tables: List[Dict[str, Any]] = []

                for table_row in table_rows:
                    table_name = table_row[0]
                    table_comment = table_row[1] or ""
                    table_key = table_name.lower()

                    # 使用领域知识库补充描述
                    table_description = table_comment or TABLE_DESCRIPTIONS.get(table_key) or ""

                    # 获取列信息
                    columns_query = text("""
                        SELECT
                            COLUMN_NAME,
                            DATA_TYPE,
                            COLUMN_COMMENT,
                            COLUMN_KEY
                        FROM INFORMATION_SCHEMA.COLUMNS
                        WHERE TABLE_SCHEMA = DATABASE()
                        AND TABLE_NAME = :table_name
                        ORDER BY ORDINAL_POSITION
                    """)
                    column_rows = conn.execute(columns_query, {"table_name": table_name}).fetchall()

                    columns: List[Dict[str, Any]] = []
                    for col_row in column_rows:
                        column_name = col_row[0]
                        data_type = col_row[1]
                        column_comment = col_row[2] or ""
                        column_key = col_row[3]

                        # 使用领域知识库补充描述
                        column_desc_key = (table_key, column_name.lower())
                        column_description = column_comment or COLUMN_DESCRIPTIONS.get(column_desc_key) or ""

                        columns.append({
                            "column_name": column_name,
                            "data_type": data_type,
                            "description": column_description,
                            "is_primary_key": column_key == "PRI",
                            "is_foreign_key": column_key in ("MUL", "FK"),
                            "is_unique": column_key == "UNI",
                        })

                    tables.append({
                        "table_name": table_name,
                        "description": table_description,
                        "columns": columns,
                    })

                # 获取外键关系
                fk_query = text("""
                    SELECT
                        kcu.TABLE_NAME as source_table,
                        kcu.COLUMN_NAME as source_column,
                        kcu.REFERENCED_TABLE_NAME as target_table,
                        kcu.REFERENCED_COLUMN_NAME as target_column
                    FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                    WHERE kcu.TABLE_SCHEMA = DATABASE()
                    AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
                    ORDER BY kcu.TABLE_NAME, kcu.COLUMN_NAME
                """)
                fk_rows = conn.execute(fk_query).fetchall()

                relationships: List[Dict[str, Any]] = []
                seen_keys: Set[Tuple[str, str, str, str]] = set()

                for fk_row in fk_rows:
                    key = (fk_row[0], fk_row[1], fk_row[2], fk_row[3])
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)

                    relationships.append({
                        "source_table": fk_row[0],
                        "source_column": fk_row[1],
                        "target_table": fk_row[2],
                        "target_column": fk_row[3],
                        "relationship_type": "FOREIGN_KEY",
                        "description": f"{fk_row[0]}.{fk_row[1]} references {fk_row[2]}.{fk_row[3]}",
                    })

                # 添加领域知识关系
                table_name_lookup = {t["table_name"].lower(): t["table_name"] for t in tables}
                for relationship in RELATIONSHIP_FACTS:
                    source_lower = relationship["source_table"].lower()
                    target_lower = relationship["target_table"].lower()
                    if source_lower not in table_name_lookup or target_lower not in table_name_lookup:
                        continue

                    source_name = table_name_lookup[source_lower]
                    target_name = table_name_lookup[target_lower]

                    key = (
                        source_name,
                        relationship["source_column"],
                        target_name,
                        relationship["target_column"],
                    )
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)

                    relationships.append({
                        "source_table": source_name,
                        "source_column": relationship["source_column"],
                        "target_table": target_name,
                        "target_column": relationship["target_column"],
                        "relationship_type": relationship.get("relationship_type", ""),
                        "description": relationship.get("description", ""),
                    })

                return tables, relationships
        finally:
            engine.dispose()

    # 在线程池中执行同步查询
    tables, relationships = await asyncio.to_thread(_sync_retrieve)

    # 根据问题关键词过滤表
    keywords = _extract_keywords(question)
    if keywords:
        scored_tables = [(table, _score_table(table, keywords)) for table in tables]
        positive = [item for item in scored_tables if item[1] > 0]
        if positive:
            positive.sort(key=lambda item: item[1], reverse=True)
            max_tables = 6
            tables = [item[0] for item in positive[:max_tables]]

            # 过滤关系，只保留相关表的关系
            table_names = {table["table_name"] for table in tables}
            relationships = [
                rel for rel in relationships
                if rel["source_table"] in table_names and rel["target_table"] in table_names
            ]

    schema_context = {
        "tables": tables,
        "relationships": relationships,
    }
    return schema_context
