# -*- coding: utf-8 -*-
"""R-P2-4 dry-run 校验（rag-optimization §6，F2b-c）。

对词法/安全检查通过的 SQL 跑 `EXPLAIN` 试跑（MySQL 方言；in-repo 先例 =
Cypher `EXPLAIN` 试跑 `components/cypher_tools/utils.py`）：

- 试跑失败 ⇒ (False, 错误) —— 喂进 `sql_validation` 的既有重试环；
- **基础设施故障**（连不上库、账号/库名错误等）⇒ (True, "") fail-open ——
  数据库不可达不能怪 SQL 本体，绝不因此误杀合法查询；
- 本模块只在 `TEXT2SQL_DRY_RUN_ENABLED=true` 时被调用（默认关 = 基线逐位等价）。
"""

from __future__ import annotations

import logging

from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)

# 基础设施故障特征（MySQL 经 SQLAlchemy 抛出时的常见文案）；
# 命中 ⇒ fail-open 放行，其余按 SQL 自身错误处理。
_INFRA_MARKERS = (
    "can't connect",
    "connection",
    "connect",
    "access denied",
    "unknown database",
    "timeout",
    "timed out",
    "name or service not known",
    "link failure",  # Communications link failure（复数/单数均覆盖）
    "too many connections",
)


def _is_infra_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _INFRA_MARKERS)


def dry_run_explain(sql: str) -> tuple[bool, str]:
    """``EXPLAIN`` 试跑。返回 ``(ok, error_message)``。

    基础设施故障 fail-open ⇒ ``(True, "")``；SQL 自身错误 ⇒ ``(False, 摘要)``。
    """
    from agentix.config import settings

    conn_str = getattr(settings, "DATABASE_URL", "") or ""
    if not conn_str or not (sql or "").strip():
        return True, ""
    try:
        engine = create_engine(conn_str)
        try:
            with engine.connect() as conn:
                conn.execute(text(f"EXPLAIN {sql}"))
        finally:
            engine.dispose()
        return True, ""
    except Exception as exc:  # noqa: BLE001 - 区分基础设施故障与 SQL 错误
        if _is_infra_error(exc):
            logger.warning("dry-run skipped (infra failure, fail-open): %s", exc)
            return True, ""
        logger.info("dry-run rejected SQL (feeding retry loop): %s", exc)
        return False, f"dry-run（EXPLAIN）未通过：{str(exc)[:300]}"
