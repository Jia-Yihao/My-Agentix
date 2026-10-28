"""T21（规划 approval-recovery · 运行态落库）——控制平面运行态表 ``agent_run_states``。

一行 = 一次 Agent 运行（``ControlPlane`` 的 ``AgentRun``）的**逐字段投影**（同上
``hitl_pending.py`` 的「接通不是重写」口径）：``run_id`` 主键、``agent_name``、
``state``（与 ``RunState.value`` 同词表：pending/running/succeeded/failed/cancelled）、
``trace_id``、``meta`` JSON、``run_depth``、``created_at`` / ``updated_at``。

``started_at`` / ``ended_at`` / ``error`` / ``cancel_reason`` 四列不是新概念，而是
``AgentRun`` 同一生命周期字段的投影：没有它们，重启后回落 GET 只能看到终态取值，
看不到「什么时候结束的」「为什么失败」——而失败归因正是运维查历史 run 的目的。

**不发散**：不落 ``budget``（进程内 ``TokenBudget`` 对象，重启后没有归属意义，回落
``null``）、不落 ``duration_ms``（由 ``started_at``/``ended_at`` 即时算出）、不落
``_task``（asyncio 任务不可序列化）。

**为什么模型不进 ``db/models/__init__.py`` 聚合**：聚合会被 ``init_db(create_all=True)``
全量建表——而纪律 1 要求 flag-off（默认）连表都不建。建表只经 store 的
``ensure_ready()``（``CREATE TABLE IF NOT EXISTS`` 幂等）在 flag-on 的装配点发生，
与 T18 的 ``hitl_pending`` 同一套口径。

**状态词表**：``state`` 列的取值域就是 :class:`agentix.domain.runtime.runs.RunState`
的值域，store 写入前显式校验（词表外的值直接抛错，不在库里留一个没人认识的终态）。

**保留策略**：本卡**不设** TTL / 行数上限（内存面 FIFO 500 淘汰的是进程内存，
库是「跑过没有」的历史面，不该跟着进程生命周期缩水）。清理/归档策略不在本卡，
不冒充已有——容量规划按「每 run 一行、只在迁移时 update」估算。

**回滚方式**：``DROP TABLE agent_run_states`` + 关 ``RUN_STATE_PERSIST_ENABLED``。
"""

from sqlalchemy import Column, Float, Integer, JSON, String, Text

from agentix.infrastructure.core.database import Base

__all__ = ["AgentRunState"]


class AgentRunState(Base):
    """一行 = 一次 Agent 运行的状态投影（创建 + 每次状态迁移更新同一行）。"""

    __tablename__ = "agent_run_states"

    #: 运行 id（``uuid4().hex`` 或调用方指定）——主键，GET 按它点名
    run_id = Column(String(64), primary_key=True)
    agent_name = Column(String(255), nullable=False, default="")
    #: pending | running | succeeded | failed | cancelled（与 RunState.value 同源）
    state = Column(String(16), nullable=False, default="pending", index=True)
    trace_id = Column(String(128), nullable=False, default="-")
    #: 运行登记面 ``meta`` 原样 JSON（含 depth / task_preview / thread_id / result 等）
    meta = Column(JSON, nullable=False, default=dict)
    #: 委托深度（登记时 ``meta.depth`` 的列化——按深度检索不必解析 JSON）
    run_depth = Column(Integer, nullable=False, default=0)
    #: 运行登记时刻（epoch 秒）；upsert 时**不覆盖**（它是这一行的生命起点）
    created_at = Column(Float, nullable=False)
    #: 最近一次写入时刻（epoch 秒）；列表回落的排序键
    updated_at = Column(Float, nullable=False, index=True)
    started_at = Column(Float, nullable=True)
    ended_at = Column(Float, nullable=True)
    #: 失败原因（``finish(FAILED, error=...)`` 原文）
    error = Column(Text, nullable=True)
    #: 取消原因（``cancel(reason=...)``）
    cancel_reason = Column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<AgentRunState {self.run_id} agent={self.agent_name} state={self.state}>"
