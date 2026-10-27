"""LG-3-4：恢复期幂等的「已完成步骤」清单（表 ``run_step_ledger``）。

为什么需要一张表：`execute_task` 节点内发生 in-loop HITL 挂起时，恢复会让
**挂起点之前的步骤重执行**（LangGraph 节点重入，`task_execution` 波循环里
已有注释登记）。步骤带工具副作用（写文件/发通知），重放就是重复副作用。
checkpoint 只记住「节点没跑完」，记不住「哪几步已经成功」；所以另立一份
完成清单，恢复时按它跳过。

键为什么是三列：设计口径是「键含 thread + step」，而同一线程会在多轮对话里
复用，仅 thread + step 会让**下一轮**的相同步骤被误跳过。第三列 ``scope`` 取
LangGraph 注入的 ``checkpoint_ns``：同一次挂起的续跑拿到同一个 ns，新一轮
对话换新 ns（1.2.18 实测）。step 身份不取模型自拟的 ``id``：恢复会重新拆解，
id/顺序不保证复现；改取 (tool, 规范化 args) 的指纹 + 同指纹内序号。

**不进** ``db/models/__init__.py`` 聚合：聚合走 ``init_db(create_all=True)``
全量建表，而 flag-off（默认）要求连表都不建。建表只经 store 的
``ensure_ready()``（``create(checkfirst=True)`` 幂等）在 flag-on 首次使用时发生。

**回滚方式**：``DROP TABLE run_step_ledger``（无外键指向它；flag-off 后不再触达）。
"""

from sqlalchemy import Column, Float, Integer, String, Text

from agentix.infrastructure.core.database import Base

__all__ = ["RunStepLedgerEntry"]


class RunStepLedgerEntry(Base):
    """一行 = 某线程某个节点执行作用域里已完成的一步。"""

    __tablename__ = "run_step_ledger"

    #: 会话/线程 id（图运行面）
    thread_id = Column(String(128), primary_key=True)
    #: 节点执行作用域（LangGraph ``checkpoint_ns``）；续跑不变、新一轮变化
    scope = Column(String(255), primary_key=True)
    #: 步骤身份：`sha256(tool, 规范化 args)[:24]` + `:序号`
    step_key = Column(String(64), primary_key=True)
    #: 工具名（排障与审计用，不参与键）
    tool = Column(String(128), nullable=False, default="")
    #: 工具返回值（JSON 文本；恢复时用来重建观测，避免重跑）
    value_json = Column(Text, nullable=False, default="null")
    #: 原始批执行的尝试次数（跳过时如实写回观测）
    attempts = Column(Integer, nullable=False, default=1)
    created_at = Column(Float, nullable=False, default=0.0)
