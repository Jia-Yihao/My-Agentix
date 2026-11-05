"""LG-3-4：已完成步骤清单的持久化通道（``run_step_ledger`` 的 repo）。

层次与惯例照 ``hitl_pending_store.py``：SQLAlchemy 同步 Session、本仓无 alembic
（建表用 ``create(checkfirst=True)``）、生产缺省**操作时刻**才解析共享惰性
engine（构造不建池、不连库）。

**失败姿态**：本 store 只显式抛错，不吞不降级；降级只在调用方一处（
``task_execution`` 捕获后按「无恢复保护」继续跑，``logger.warning`` 留痕，
约定 2）。理由与 hitl store 相同：同一个判定不许两个源。

**只读路径不造表**：``load_scope`` 也先 ``ensure_ready``——表不存在时恢复
本来就需要「没有已完成步骤」的答案，建一张空表再返回空是同一语义，却省掉
调用方区分「表缺失」与「库挂了」的分支。
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, Iterator, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from agentix.infrastructure.persistence.db.models.run_step_ledger import (
    RunStepLedgerEntry,
)

__all__ = [
    "StepLedgerStore",
    "get_step_ledger_store",
    "reset_step_ledger_store",
]


def _dump(value: Any) -> str:
    """返回值 → JSON 文本。非 JSON 友好对象按 ``str`` 落账（跳过后观测只能用文本）。"""
    return json.dumps(value, ensure_ascii=False, default=str)


class StepLedgerStore:
    """``run_step_ledger`` 的 repo：按 (thread, scope) 读一作用域、按步 upsert。"""

    def __init__(self, *, session_factory: Optional[sessionmaker] = None) -> None:
        self._session_factory = session_factory
        self._ready = False

    # -- 连接面 -------------------------------------------------------------

    def _resolve_factory(self) -> sessionmaker:
        if self._session_factory is not None:
            return self._session_factory
        from agentix.infrastructure.core.database import get_session_factory

        return get_session_factory()

    @contextmanager
    def _session(self) -> Iterator[Session]:
        self.ensure_ready()
        session = self._resolve_factory()()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def ensure_ready(self) -> None:
        if self._ready:
            return
        engine = self._resolve_factory().kw.get("bind")
        if engine is None:
            from agentix.infrastructure.core.database import get_engine

            engine = get_engine()
        RunStepLedgerEntry.__table__.create(bind=engine, checkfirst=True)
        self._ready = True

    # -- 读写面 -------------------------------------------------------------

    def load_scope(self, thread_id: str, scope: str) -> Dict[str, Dict[str, Any]]:
        """读一作用域内的全部已完成步骤：``{step_key: {tool, value, attempts}}``。

        单行载荷损坏按**未命中**处理（该步会重跑，不会因一条坏行让整轮失败）；
        损坏行不删——写入方覆盖它，或运维按日志清理。
        """
        if not thread_id or not scope:
            return {}
        with self._session() as session:
            rows = session.execute(
                select(RunStepLedgerEntry).where(
                    RunStepLedgerEntry.thread_id == thread_id,
                    RunStepLedgerEntry.scope == scope,
                )
            ).scalars()
            done: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                try:
                    value = json.loads(row.value_json)
                except (TypeError, ValueError):
                    continue
                done[row.step_key] = {
                    "tool": row.tool,
                    "value": value,
                    "attempts": int(row.attempts or 1),
                }
            return done

    def mark_many(
        self,
        thread_id: str,
        scope: str,
        entries: Iterable[Mapping[str, Any]],
    ) -> int:
        """把一批成功步骤 upsert 进清单；返回写入行数（空批不建表）。

        ``entries`` 的键：``step_key`` / ``tool`` / ``value`` / ``attempts``。
        用 ``merge``（按三列主键 upsert）而非 insert——恢复期同一作用域会重复
        经过同一批步骤，重复写必须是幂等的。
        """
        rows = list(entries)
        if not thread_id or not scope or not rows:
            return 0
        now = time.time()
        with self._session() as session:
            for entry in rows:
                session.merge(
                    RunStepLedgerEntry(
                        thread_id=thread_id,
                        scope=scope,
                        step_key=str(entry["step_key"]),
                        tool=str(entry.get("tool") or ""),
                        value_json=_dump(entry.get("value")),
                        attempts=max(1, int(entry.get("attempts") or 1)),
                        created_at=now,
                    )
                )
        return len(rows)


_store_lock = threading.Lock()
_store: Optional[StepLedgerStore] = None


def get_step_ledger_store() -> StepLedgerStore:
    """进程级单例（构造不连库；首次读写在操作时刻解析 engine）。"""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = StepLedgerStore()
    return _store


def reset_step_ledger_store(*, session_factory: Optional[sessionmaker] = None) -> StepLedgerStore:
    """重建单例（测试注入 sqlite 内存 engine；生产启动路径不调用）。"""
    global _store
    with _store_lock:
        _store = StepLedgerStore(session_factory=session_factory)
        return _store
