"""T18（规划 approval-recovery Q7 裁决：Postgres 为真源）——HITL pending 审批表。

字段载体就是桥侧既有序列化面的**逐字段投影**（不另造格式，纪律：接通不是重写）：

- ``sidecar_*`` 列：``SidecarReviewRequest.to_public_dict()`` 的拆列
  （结构化审面，**无** Agent trajectory / narrative，sidecar 上下文隔离承诺不变）；
- ``gate_request`` 列：``ReviewRequest.to_dict()`` 原样 JSON——跨实例/重启裁决时
  经 ``hitl_bridge._request_from_dict`` 同式重建；
- 裁决列（status/decided_*/edited_arguments）：裁决闭合面，词汇与桥的裁决桶
  ``{approved, rejected, edited, resumed}`` 逐字一致（同一概念一种叫法）。

**为什么模型不进 ``db/models/__init__.py`` 聚合**：聚合会被
``init_db(create_all=True)`` 全量建表——而纪律要求 flag-off（默认）**连表都不建**
（纪律 1：不读写新表，建表同样属"新表触达"）。建表只经 store 的
``ensure_ready()``（``CREATE TABLE IF NOT EXISTS`` 幂等）在 flag-on 的装配点发生。

**本模块两张表**（同一套建表/回滚口径，都由 store 的 ``ensure_ready()`` 建）：

- ``hitl_pending_approvals``：一行 = 一条审批 pending 的真源生命周期
  （挂起写入 → 裁决闭合）。行型不止这一种：另有 ``nodeerr:*`` **补偿行**，
  id 由 ``agentix/orchestration/langgraph/policies.py`` 的生成点（现读该文件
  第 158 行）以 ``nodeerr:`` 前缀命名，**没有挂起阶段**——它跳过 pending，
  由 store 的 ``close_decision()``（``agentix/infrastructure/persistence/hitl_pending_store.py``，
  现读该文件第 336 行的定义）直接写闭合。按"每行都经历挂起→闭合"的旧定义
  写的清理/回滚/扫描逻辑会把补偿行当残留误删，所以任何按行型筛的清理
  都必须同时考虑这一类；
- ``hitl_sweep_leases``：一行 = 一把**命名租约**（当前只有
  ``hitl_approval_deadline`` 这一把）。存在的理由是部署形态：
  ``--workers 2`` 意味着两个独立进程各起一个到期扫描任务，而"同一行只被
  裁决一次"不能靠"恰好只有一个扫描者"来保证——那是个未声明的前提。

**status 词表不变**：``pending | approved | rejected | edited | resumed``。
行认领（``claim_owner`` / ``claim_until``）是**列级 CAS 标记**，只用于并发去重，
不进状态机——把"认领中"写进 status 会凭空多出一个既非未决也非已决的状态，
所有按 status 判"是否可裁决"的既有读路径都得跟着改。

**回滚方式**：``DROP TABLE hitl_pending_approvals`` 与 ``DROP TABLE
hitl_sweep_leases``（无其他表外键指向它们，flag-off 后两张表都不再被触达）。

**溢出策略口径**（卡内"风险与口径冲突"要求写死的一边）：**库侧同样封顶**，
默认未决 200 行，与内存 FIFO 的 ``max_requests=200`` 保持同一数字——真源表被
无限撑大是更大的事故面。与内存的差异在"挡门的姿态"：内存挤掉最老是静默的，
库挡门**必须显式**（抛 ``pending_store_full``，由桥落回执标记 + error 日志），
绝不静默挤掉任何一条未决真源行；已决（闭合）行不计入未决上限，当前**不另设
上限**——保留/清理策略属后续卡，本卡不冒充已有。
"""

from sqlalchemy import Column, Float, JSON, String, Text

from agentix.infrastructure.core.database import Base

__all__ = ["HitlPendingApproval", "HitlSweepLease"]


class HitlPendingApproval(Base):
    """一行 = 一条审批 pending 的真源生命周期（挂起写入 → 裁决闭合）。

    含 ``nodeerr:*`` 补偿行：无挂起阶段、由 store 的 ``close_decision()``
    直写闭合（完整口径见模块 docstring 的行型说明）。
    """

    __tablename__ = "hitl_pending_approvals"

    #: Sidecar 请求 id（uuid hex）——主键；ops 裁决端点按它点名
    sidecar_request_id = Column(String(64), primary_key=True)
    #: 闸门请求 id（ReviewRequest.request_id），跨栈映射（桥 ``_map`` 的库化）
    gate_request_id = Column(String(64), nullable=False, index=True)
    #: 挂起所在线程（图/会话面），供恢复与排障定位；钩子路径可为空串
    thread_id = Column(String(128), nullable=False, default="")
    tool_name = Column(String(255), nullable=False)
    #: 声明级（L0–L3），与 SidecarReviewRequest.declared_risk_level 同词表
    declared_risk_level = Column(String(8), nullable=False, default="L0")
    #: pending | approved | rejected | edited | resumed（与桥裁决桶同源）
    status = Column(String(16), nullable=False, default="pending", index=True)
    #: 结构化参数（送审面投影；短值摘要闸在执行点已施加）
    arguments = Column(JSON, nullable=False, default=dict)
    evidence_ids = Column(JSON, nullable=False, default=list)
    artifact_refs = Column(JSON, nullable=False, default=list)
    #: ReviewRequest.to_dict() 原样载荷（重启/跨实例重建闸门记录用）
    gate_request = Column(JSON, nullable=True)
    created_at = Column(Float, nullable=False)
    decided_at = Column(Float, nullable=True)
    decided_by = Column(String(128), nullable=True)
    decision_reason = Column(Text, nullable=True)
    #: EDIT 裁决的合并后参数（与 BridgeDecision.merged_arguments 同源）
    edited_arguments = Column(JSON, nullable=True)
    #: 行认领者（进程标识）：非空且未过期 = 该行正被某个扫描者裁决。
    #: 认领是「条件更新」的 CAS 标记，只用于并发去重，不改变 status 词表。
    claim_owner = Column(String(128), nullable=False, default="")
    #: 认领租约到期时刻（epoch）。到期后可由其他扫描者接管（进程崩溃的自愈路径）。
    claim_until = Column(Float, nullable=False, default=0.0)

    def __repr__(self) -> str:  # 不带 arguments：审批载荷可能含敏感参数值
        return (
            f"<HitlPendingApproval {self.sidecar_request_id} "
            f"tool={self.tool_name} status={self.status}>"
        )


class HitlSweepLease(Base):
    """扫描租约：同一时刻只允许一个进程周期性扫描到期 pending。

    ``--workers 2`` 下每个进程都会起一个扫描任务；没有这把租约，两个扫描者
    同时扫到同一行只是时间问题。租约带 ``expires_at`` 而不是靠"谁先起谁持有"：
    持有者进程崩溃时没人会来释放，必须由时间到期让**别的** worker 接管。
    """

    __tablename__ = "hitl_sweep_leases"

    #: 租约名（一行业务语义一把租约，当前只有 ``hitl_approval_deadline``）
    name = Column(String(64), primary_key=True)
    #: 持有者标识（host:pid:uuid8，跨进程可区分、进程内稳定）
    owner = Column(String(128), nullable=False, default="")
    #: 租约到期时刻（epoch）；到期即可被其他 owner 条件更新接管
    expires_at = Column(Float, nullable=False, default=0.0)
