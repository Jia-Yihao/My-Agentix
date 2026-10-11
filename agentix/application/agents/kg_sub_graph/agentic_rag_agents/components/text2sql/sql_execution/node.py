"""
SQL execution node.
"""

import asyncio
from typing import Any, Callable, Coroutine, Dict, List, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from agentix.config import settings
from agentix.infrastructure.core.logger import get_logger
from agentix.infrastructure.text2sql.wren_adapter import (
    WrenNotAvailable,
    WrenSQLBackend,
    get_wren_backend,
)

logger = get_logger(service="text2sql.sql_execution")


def create_sql_execution_node(
    connection_string: Optional[str] = None,
) -> Callable[[Dict[str, Any]], Coroutine[Any, Any, Dict[str, Any]]]:
    """
    Build a LangGraph node that executes read-only SQL statements.
    """

    async def execute_sql(state: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("-----开始执行 SQL 查询-----")

        sql_statement = (state.get("sql_statement") or "").strip()
        connection_id = state.get("connection_id")
        max_rows = int(state.get("max_rows") or 1000)

        if not sql_statement:
            logger.warning("SQL 语句为空，跳过执行")
            return {
                "execution_results": None,
                "execution_error": "SQL 语句为空",
                "steps": ["sql_execution_skipped"],
            }

        if not _is_read_only_query(sql_statement):
            logger.warning("检测到非只读查询，已阻止执行")
            return {
                "execution_results": None,
                "execution_error": "仅支持只读 SELECT 查询",
                "steps": ["sql_execution_blocked"],
            }

        if not state.get("is_valid"):
            logger.warning("SQL 验证未通过，跳过执行")
            return {
                "execution_results": None,
                "execution_error": "SQL 验证未通过",
                "steps": ["sql_execution_skipped_invalid"],
            }

        conn_str = connection_string or _get_connection_string(connection_id)
        if not conn_str:
            logger.error("无法获取数据库连接信息 connection_id=%s", connection_id)
            return {
                "execution_results": None,
                "execution_error": "无法获取数据库连接信息",
                "steps": ["sql_execution_failed"],
            }

        # Wren 语义层后端（TEXT2SQL_BACKEND=wren 时启用）：语义校验 + 语义执行；
        # 不可用（未装 wrenai / 项目无效 / 执行失败可重试性未知）→ 降级 MySQL，
        # steps 留痕 sql_execution_wren_degraded，主链路绝不中断。
        # R-P2-4（F2b-c）：工厂返回 None 但后端被显式请求为 wren = Wren 缺失，
        # 同样按降级留痕 + 打点（此前只 logger.warning，无 steps 无指标）。
        wren_backend: Optional[WrenSQLBackend] = get_wren_backend()
        wren_degraded = False
        if wren_backend is None and settings.TEXT2SQL_BACKEND == "wren":
            logger.warning("Wren 后端已请求但不可用（工厂返回 None），MySQL 直连降级")
            wren_degraded = True
        if wren_backend is not None:
            try:
                await asyncio.to_thread(wren_backend.validate, sql_statement)
                results = await asyncio.to_thread(
                    wren_backend.execute, sql_statement, max_rows
                )
                logger.info(
                    "SQL 经 Wren 语义层执行成功，返回 %d 行结果", len(results)
                )
                return {
                    "execution_results": results,
                    "execution_error": None,
                    "steps": ["sql_execution_wren"],
                }
            except WrenNotAvailable as exc:
                logger.warning("Wren 后端不可用，降级 MySQL 直连: %s", exc)
                wren_degraded = True
            except Exception as exc:  # noqa: BLE001 - 语义校验/执行失败 → 降级
                logger.warning("Wren 执行失败（%s），降级 MySQL 直连", exc)
                wren_degraded = True

        if wren_degraded:
            # R-P2-4：Wren 缺失/降级显式打点（沿 steps sql_execution_wren_degraded，
            # 指标让降级在 Prometheus 可见——steps 此前被 tool 层丢弃）。
            try:
                from agentix.observability.metrics import record_degraded

                record_degraded("text2sql_wren")
            except Exception:  # noqa: BLE001 - 打点绝不影响执行主链路
                logger.debug("wren degraded metric failed (non-fatal)")

        try:
            results = await _execute_sql_query(conn_str, sql_statement, max_rows=max_rows)
            logger.info("SQL 执行成功，返回 %d 行结果", len(results))
            return {
                "execution_results": results,
                "execution_error": None,
                "steps": ["sql_execution_wren_degraded"] if wren_degraded else ["sql_execution"],
            }
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("SQL 执行失败: %s", exc)
            return {
                "execution_results": None,
                "execution_error": str(exc),
                "steps": ["sql_execution_failed"],
            }

    return execute_sql


# 2026-10-06 完整性审查：此处原有 _map_db_type_to_driver()，把 postgresql 系映射到
# `postgresql+psycopg2` —— 但 requirements.txt 只声明 `psycopg[binary]`（psycopg3），
# psycopg2 仅是本机历史遗留、未被任何声明的包以非 extra 方式带入，全新安装会
# NoSuchModuleError。该函数全仓无调用者（_get_connection_string 直接返回
# settings.DATABASE_URL，驱动由 URL scheme 决定），属死代码，故整段删除。
def _get_connection_string(connection_id: Optional[int]) -> Optional[str]:
    """返回 Text2SQL 的目标库连接串，即应用自身的 ``settings.DATABASE_URL``。

    不再查 dbconnection 表、不再按 db_type 换驱动：驱动名内嵌在 URL scheme 里
    （如 ``postgresql+psycopg`` / ``mysql+pymysql``），与 requirements.txt 声明一致。
    """
    logger.debug("使用应用 DATABASE_URL 作为 Text2SQL 目标库: %s", settings.DATABASE_URL)
    return settings.DATABASE_URL


async def _execute_sql_query(
    connection_string: str,
    sql: str,
    max_rows: int = 1000,
) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(_run_query_sync, connection_string, sql, max_rows)


def _run_query_sync(
    connection_string: str,
    sql: str,
    max_rows: int,
) -> List[Dict[str, Any]]:
    engine: Engine = create_engine(connection_string, future=True)
    try:
        with engine.connect() as connection:
            result = connection.execution_options(stream_results=True).execute(text(sql))
            columns = list(result.keys())
            rows = result.fetchmany(max_rows)
            return [
                {column: row[idx] for idx, column in enumerate(columns)}
                for row in rows
            ]
    finally:
        engine.dispose()


def _is_read_only_query(sql: str) -> bool:
    """
    检查 SQL 是否为只读查询（SELECT、WITH、EXPLAIN）

    Returns:
        True if query is read-only, False otherwise
    """
    statement = sql.strip()
    if not statement:
        return False

    # 检查是否包含多条语句（;分隔）
    if ";" in statement.rstrip(";"):
        return False

    upper = statement.upper()

    # 允许的只读关键词
    readonly_keywords = ['SELECT ', 'WITH ', 'EXPLAIN ', 'SHOW ']
    starts_with_readonly = any(upper.startswith(kw) for kw in readonly_keywords)

    # 危险关键词（即使在SELECT中也不允许）
    dangerous_keywords = [
        'INSERT ', 'UPDATE ', 'DELETE ', 'DROP ', 'CREATE ', 'ALTER ',
        'TRUNCATE ', 'REPLACE ', 'MERGE ', 'GRANT ', 'REVOKE ', 'EXEC ',
        'EXECUTE ', 'CALL '
    ]
    contains_dangerous = any(kw in upper for kw in dangerous_keywords)

    return starts_with_readonly and not contains_dangerous
