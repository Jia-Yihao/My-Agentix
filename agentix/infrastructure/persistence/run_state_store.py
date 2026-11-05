"""T21（规划 approval-recovery Q7 裁决：Postgres 为真源）——运行态落库连接面。

层次与惯例同 ``hitl_pending_store.py``：``agentix/infrastructure/persistence/`` 下的
基础设施 repo，载体是 **SQLAlchemy 同步 Session**；本仓无 alembic，建表照
``Table.create(checkfirst=True)`` 幂等惯例（``ensure_ready``），不引入迁移框架。

**同步实现、显式抛错**（同 T18）：写/读失败如何被观察到由调用方决定——
``ControlPlane`` 的后台写线程负责 warning + ``stats().persist_errors`` 计数（约定 2
fail-open：写库失败绝不阻断运行、绝不回滚内存态），HTTP 读路径负责 warning 后回落
内存面。降级判据只在调用方一处，本层不吞异常。

**与 T18 的失败语义差异（不得互相援引）**：T18 的审批 pending 是**治理真源**，
落库失败要显式回执标记、不许静默降级成纯内存「成功」；本卡是**运行态观测面**，
落库失败只降级该次写（运行照跑），可见性由 warning + 计数键承担。两者措辞相邻，
语义不同：一个管「批了没有」，一个管「跑过没有」。

**惰性连接**（§24.2 G10 口径）：缺省 session 工厂在**操作时刻**才经
``agentix.infrastructure.core.database.get_session_factory()`` 取共享惰性 engine；
构造 store 不建池、不连库（flag-off 时连本模块都不 import，见
``control_plane.resolve_run_state_store``）。

**并发口径**：单进程内由 ``ControlPlane`` 的**单写线程**串行写（不并发）；跨进程
（``--workers 2``）可能撞主键——``save_run`` 对 ``IntegrityError`` 重试一次（此时
行已由对端插入，走更新分支）。不做「先读后写的正确性保证」之外的花活：运行态是
「最后一次写入者赢」，与内存态一致。
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from agentix.domain.runtime.runs import RunState
from agentix.infrastructure.persistence.db.models.agent_run_state import AgentRunState

__all__ = [
    "RUN_STATE_VALUES",
    "DEFAULT_LIST_LIMIT",
    "RunStateStore",
    "build_default_run_state_store",
]

#: 库里允许出现的状态取值 = ``RunState`` 值域（同一概念一种叫法；词表外直接拒写）
RUN_STATE_VALUES = frozenset(s.value for s in RunState)

#: 列表回落默认上限（有界读取：历史 run 无上限时一次把库拉穿没有意义）
DEFAULT_LIST_LIMIT = 200


class RunStateStore:
    """``agent_run_states`` 的 repo（``ControlPlane`` 的库化通道）。

    ``session_factory`` 注入是测试面（sqlite 内存 engine）；生产缺省 None ⇒
    操作时刻解析共享惰性 engine。所有方法**同步、显式抛错**。
    """

    def __init__(self, *, session_factory: Optional[sessionmaker] = None) -> None:
        self._session_factory = session_factory
        self._ready = False

    # -- 连接面 -------------------------------------------------------------

    def _resolve_factory(self) -> sessionmaker:
        if self._session_factory is not None:
            return self._session_factory
        # 惰性 import + 惰性取工厂：构造/装配时刻绝不建池（G10）
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
        """只建本卡这一张表（``checkfirst`` 幂等）；仅 flag-on 装配路径会走到这里。

        刻意不进 ``Base.metadata.create_all`` 全量：全量会把别的域表也带出来，
        既超出本任务边界，也模糊「表只在 flag-on 时创建」的回滚口径。
        """
        if self._ready:
            return
        engine = self._resolve_factory().kw.get("bind")
        if engine is None:
            from agentix.infrastructure.core.database import get_engine

            engine = get_engine()
        AgentRunState.__table__.create(bind=engine, checkfirst=True)
        self._ready = True

    # -- 写路径 -------------------------------------------------------------

    def save_run(self, record: Dict[str, Any]) -> None:
        """upsert 一行运行态（主键冲突则更新状态/元数据/时刻）。

        ``record`` 形状 = ``AgentRun.to_record()``（见 ``runtime/control_plane.py``）：
        ``run_id`` / ``agent_name`` / ``state`` / ``trace_id`` / ``meta`` /
        ``run_depth`` / ``created_at`` / ``updated_at`` / ``started_at`` /
        ``ended_at`` / ``error`` / ``cancel_reason``。

        ``state`` 词表外 ⇒ ``ValueError``（宁可这次写失败，也不在库里留下一个
        没人认识的终态）；``meta`` 里出现不可 JSON 化的对象 ⇒ 按 ``str`` 降级一次
        （执行器返回值可能是任意对象，而「终态必须落」优先于「原样落」）。
        """
        rid = str((record or {}).get("run_id") or "")
        if not rid:
            raise ValueError("save_run requires a non-empty run_id")
        state = str((record or {}).get("state") or "")
        if state not in RUN_STATE_VALUES:
            raise ValueError(
                f"invalid run state {state!r}; must be one of {sorted(RUN_STATE_VALUES)}"
            )
        payload = dict(record or {})
        payload["meta"] = _json_safe_meta(payload.get("meta"))
        try:
            with self._session() as session:
                self._upsert(session, rid, payload, state)
        except IntegrityError:
            # 跨进程首启竞争：对端刚插进同主键（同一 run_id 被显式指定时的罕见形态）。
            # 重试一次即走更新分支——运行态是「最后一次写入者赢」，不需要 CAS。
            with self._session() as session:
                self._upsert(session, rid, payload, state)

    @staticmethod
    def _upsert(session: Session, run_id: str, record: Dict[str, Any], state: str) -> None:
        row = session.get(AgentRunState, run_id)
        if row is None:
            row = AgentRunState(run_id=run_id)
            session.add(row)
            # created_at 只在本行第一次诞生时落定（与内存态一致：重试/接管不改起点）
            row.created_at = float(record.get("created_at") or time.time())
        row.agent_name = str(record.get("agent_name") or "")
        row.state = state
        row.trace_id = str(record.get("trace_id") or "-")
        row.meta = dict(record.get("meta") or {})
        row.run_depth = int(record.get("run_depth") or 0)
        row.updated_at = float(record.get("updated_at") or time.time())
        row.started_at = record.get("started_at")
        row.ended_at = record.get("ended_at")
        row.error = record.get("error")
        row.cancel_reason = record.get("cancel_reason")

    # -- 读路径 -------------------------------------------------------------

    def load_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """按主键取一条；不存在 ⇒ ``None``（缺失是正常值域，不抛）。"""
        with self._session() as session:
            row = session.get(AgentRunState, str(run_id or ""))
            return self._to_record(row) if row is not None else None

    def list_runs(
        self, *, state: Optional[str] = None, limit: int = DEFAULT_LIST_LIMIT
    ) -> List[Dict[str, Any]]:
        """按登记时刻倒序列出（可按状态过滤，``limit`` 有界）。

        倒序的理由：列表回落服务的是「最近跑过什么」，与内存面（spawn 序）合并时
        内存优先、库只补历史，历史里最新的排前面才符合运维的读取习惯。
        """
        stmt = select(AgentRunState)
        if state is not None:
            stmt = stmt.where(AgentRunState.state == str(state))
        stmt = stmt.order_by(AgentRunState.created_at.desc()).limit(max(1, int(limit)))
        with self._session() as session:
            rows = session.scalars(stmt).all()
            return [self._to_record(r) for r in rows]

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _to_record(row: AgentRunState) -> Dict[str, Any]:
        """行 → ``AgentRun.from_record`` 可消费的记录（不在这里做响应投影）。"""
        return {
            "run_id": row.run_id,
            "agent_name": row.agent_name,
            "state": row.state,
            "trace_id": row.trace_id,
            "meta": dict(row.meta or {}),
            "run_depth": int(row.run_depth or 0),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "started_at": row.started_at,
            "ended_at": row.ended_at,
            "error": row.error,
            "cancel_reason": row.cancel_reason,
        }


def _json_safe_meta(value: Any) -> Dict[str, Any]:
    """把 ``meta`` 收敛成可入库的 JSON（不可序列化对象降级为 ``str``）。

    为什么不是「序列化失败就整条不写」：运行终态必须落（验收②）——执行器返回值
    可能是任意 Python 对象，为它丢掉整行记录等于让「跑过没有」这个事实消失。
    只在真的序列化不了时才降级，正常路径零改动。
    """
    payload = dict(value or {}) if isinstance(value, dict) else {}
    try:
        json.dumps(payload)
    except (TypeError, ValueError):
        return json.loads(json.dumps(payload, default=str))
    return payload


def build_default_run_state_store() -> RunStateStore:
    """缺省连接面：共享惰性 engine，构造时刻不建池、不连库、不建表。

    只在 ``RUN_STATE_PERSIST_ENABLED=true`` 的装配路径被调用（判据唯一源：
    ``control_plane.resolve_run_state_store``）。
    """
    return RunStateStore(session_factory=None)
