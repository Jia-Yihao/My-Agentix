"""Text2SQL 目标库连接串的**单源**（录制 evals 基线时发现的接线缺口，§24.3 T2 附注）。

此前 schema 拉取、SQL 执行、``EXPLAIN`` 试跑、schema 缓存键四处**各自**读
``settings.DATABASE_URL``——即"应用自己的元数据库"。而 compose 给部署方配的
法律库是另一套（``MYSQL_*`` + ``data/init_mysql.sql`` 建的 ``legal_db``），
两头从来不是同一个库：于是 text2sql 分支拿着会话/产物这类**应用元数据表**
去回答"民法典有多少条"，无论模型多强都答不出正确数字。

做成单一函数而不是四个站点各写一遍 `or` 回退：连接串口径一旦分叉，
schema 从 A 库拉、SQL 却在 B 库跑，报出来的错会指向不存在的那张表。

默认（``TEXT2SQL_DATABASE_URL`` 留空）= 回退 ``DATABASE_URL`` = 与接线前
逐位等价（纪律 1）。
"""

from __future__ import annotations

from agentix.config import settings

__all__ = ["text2sql_database_url"]


def text2sql_database_url() -> str:
    """Text2SQL 真正该打的库：显式配置优先，留空回退应用自身 ``DATABASE_URL``。"""
    explicit = (getattr(settings, "TEXT2SQL_DATABASE_URL", "") or "").strip()
    return explicit or (getattr(settings, "DATABASE_URL", "") or "")
