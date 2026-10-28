"""T18（规划 approval-recovery Q7 裁决：Postgres 为真源）——HITL pending 落库连接面。

层次：``agentix/infrastructure/persistence/`` 下的基础设施 repo（与
``repositories.py`` 同层同惯例）。载体是 **SQLAlchemy 同步 Session**——本包既有
CRUD 全是同步，且**本仓无 alembic**：建表照 ``Table.create(checkfirst=True)``
幂等惯例（``ensure_ready``），不引入迁移框架（引入新依赖属硬禁）。

对桥（``hitl_bridge``）暴露的公开面全部**同步、显式抛错**：失败如何被观察到由
调用方负责（error 日志 + 回执 ``persist_error`` 标记 + ``persist_errors`` 计数，
验收⑤），这里不吃异常也不降级——降级路径只存在于桥侧一处，判据不许两个源。

惰性连接（§24.2 G10 口径）：缺省 session 工厂在**操作时刻**才经
``agentix.infrastructure.core.database.get_session_factory()`` 取共享惰性
engine；构造 store 不建池、不连库（否则 import/装配就把「默认关」变成「默认连」）。

状态词表（与桥裁决桶逐字一致，同一概念一种叫法）：
``pending`` → ``approved | rejected | edited | resumed``。

**并发去重（T19 第 2 轮返工）**：两张表各司其职——``hitl_sweep_leases`` 保证
「同一时刻只有一个扫描者」（部署形态 ``--workers 2`` 下每个进程都会起扫描任务），
``hitl_pending_approvals.claim_*`` 列保证「同一条 pending 只被一个扫描者裁决」。
两层都是**原子条件更新**（CAS），不是"先读后写"：先读后写在两个进程之间没有
任何互斥可言——两边都读到 ``pending`` 就都会去裁决，读到的那一瞬间就已经错了。
状态词表不因认领而增加取值（认领是列级标记，不是状态）。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, Iterator, List, Optional

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from agentix.infrastructure.persistence.db.models.hitl_pending import (
    HitlPendingApproval,
    HitlSweepLease,
)

__all__ = [
    "STATUS_PENDING",
    "DECIDED_STATUSES",
    "SWEEP_LEASE_NAME",
    "DEFAULT_SWEEP_LEASE_TTL_S",
    "DEFAULT_CLAIM_TTL_S",
    "HitlPendingStoreFull",
    "HitlPendingStore",
    "build_default_hitl_pending_store",
]

#: 未决态（唯一可被裁决的状态）
STATUS_PENDING = "pending"
#: 闭合态值域 = 桥 ``decide`` 的裁决桶标签
DECIDED_STATUSES = frozenset({"approved", "rejected", "edited", "resumed"})

#: 扫描租约名（一行业务语义一把租约：审批时限扫描）
SWEEP_LEASE_NAME = "hitl_approval_deadline"
#: 扫描租约默认 TTL（秒）：长度是两个坏处的折中——太短会让两个 worker 交替进出
#: 租约（谁的扫描都可能被"合法"抢占），太长会让持有者崩溃后的空窗（没人扫）变长。
#: 120 秒 ≈ 4 倍最大扫描周期（``_hitl_sweep_interval`` 上界 30 秒）。
DEFAULT_SWEEP_LEASE_TTL_S = 120.0
#: 行认领默认 TTL（秒）：与租约同量级——它的作用是"close 失败后别在下一轮立刻
#: 重算"，一轮扫描周期（≤30 秒）远小于它，故足以挡住立即重算。
DEFAULT_CLAIM_TTL_S = 120.0


class HitlPendingStoreFull(RuntimeError):
    """未决行数已达库侧上限——拒绝写入（**显式失败**，绝不静默挤行）。

    消息里刻意带 ``pending_store_full`` 标记词：桥侧把它转进回执
    ``persist_error``，运维在 API 面直接可读（验收⑤）。
    """


class HitlPendingStore:
    """``hitl_pending_approvals`` 的 repo（桥的库化通道）。

    ``session_factory`` 注入是测试面（sqlite 内存 engine）；生产缺省 None ⇒
    操作时刻解析共享惰性 engine。所有方法幂等：``save_pending`` 按主键 upsert，
    ``close_decision`` 对未见行补写闭合（裁决必须进真源，不许"库里没有就不算"）。

    **重复裁决的唯一判据在桥侧**（T19 第 4 轮返工）：本层不再"各写一份去重判据"，
    只做一条硬约束——``close_decision`` **不覆盖已决行**（``status != pending`` 即
    返回 ``False``，一个闭合列都不写）。理由：多 worker（``--workers 2``）下同一行
    可能被两个实例先后裁决，后手若照写就会把先手的 ``decided_by`` /
    ``decision_reason`` / ``status`` 一起改掉——超时归因会被一次人工点按抹成人工拒，
    点 approve 连「已超时拒绝」这个事实都没了。桥侧仍以 ``not_found`` 兜住重复裁决
    （它知道内存与闸门的状态），两层分工不同：桥判"该不该裁"，库判"能不能写回来"。
    """

    #: 未决行上限：与内存 FIFO（``HumanReviewGate.max_requests=200``）同数的口径
    #: 见 ``db/models/hitl_pending.py`` 模块注释——库侧同样封顶，但挡门必须显式。
    DEFAULT_MAX_UNRESOLVED = 200

    def __init__(
        self,
        *,
        session_factory: Optional[sessionmaker] = None,
        max_unresolved: int = DEFAULT_MAX_UNRESOLVED,
    ) -> None:
        self._session_factory = session_factory
        self.max_unresolved = max(1, int(max_unresolved))
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
            # 显式失败上抛：吞这里 = 验收⑤说的"静默降级"，禁止
            raise
        finally:
            session.close()

    def ensure_ready(self) -> None:
        """只建本卡这两张表（``checkfirst`` 幂等）；仅 flag-on 装配路径会走到这里。

        刻意不进 ``Base.metadata.create_all`` 全量：全量会把别的域表也带出来，
        超出本任务边界（最小改动），也模糊"表只在 flag-on 时创建"的回滚口径。
        """
        if self._ready:
            return
        engine = self._resolve_factory().kw.get("bind")
        if engine is None:
            from agentix.infrastructure.core.database import get_engine

            engine = get_engine()
        HitlPendingApproval.__table__.create(bind=engine, checkfirst=True)
        HitlSweepLease.__table__.create(bind=engine, checkfirst=True)
        self._ready = True

    # -- 并发去重：扫描租约 ------------------------------------------------

    def acquire_sweep_lease(
        self,
        owner: str,
        *,
        ttl_s: float = DEFAULT_SWEEP_LEASE_TTL_S,
        now: Optional[float] = None,
    ) -> bool:
        """尝试取得（或续租）扫描租约；``False`` = 别人持有且未到期。

        为什么是条件更新而不是"先读 owner 再决定写"：两个 worker 会同时读到
        "租约已过期"，然后都去写——读和写之间没有任何互斥，两个扫描者就这么起来了。
        把判据与写入压进**同一条 UPDATE**（``WHERE owner=me OR expires_at<=now``），
        数据库的行锁 + ``rowcount`` 就是这个"谁抢到"的唯一裁决点。
        ``owner=me`` 也在条件里：同一进程需要能续租（扫描轮次可能长于 TTL）。

        首启是靠 INSERT：UPDATE 命中 0 行时先查有没有行，没有才插。并发首启会撞
        主键 ``IntegrityError``——那正是"别人先插成功了"，属正常值域，返回 ``False``。
        """
        who = str(owner or "")
        at = float(now if now is not None else time.time())
        expires = at + max(0.0, float(ttl_s))
        with self._session() as session:
            updated = session.execute(
                update(HitlSweepLease)
                .where(
                    HitlSweepLease.name == SWEEP_LEASE_NAME,
                    or_(HitlSweepLease.owner == who, HitlSweepLease.expires_at <= at),
                )
                .values(owner=who, expires_at=expires)
                # 不在会话内做对象同步：这里要的就是数据库侧的 matched-row 计数
                # （谁抢到的唯一裁决点），多余的一次 SELECT 只会给"读后再判"留缝。
                .execution_options(synchronize_session=False)
            )
            if updated.rowcount == 1:
                return True
            if session.get(HitlSweepLease, SWEEP_LEASE_NAME) is not None:
                return False
            session.add(HitlSweepLease(name=SWEEP_LEASE_NAME, owner=who, expires_at=expires))
            try:
                session.flush()
            except IntegrityError:
                # 并发首启竞争：另一个 worker 刚插进同主键。**本函数就地** rollback
                # 并返回 False（不上抛）——"没抢到"是正常值域，不是故障；`_session`
                # 随后执行的那次 commit 作用在已回滚的空事务上，等价于空操作。
                session.rollback()
                return False
            return True

    def release_sweep_lease(self, owner: str) -> bool:
        """让出租约（关停路径调用）；``False`` = 本就不由 ``owner`` 持有。

        只删"自己的那把"：``WHERE owner=me`` 保证一个慢速 worker 不会误删别人
        刚接管的租约（那等于把唯一扫描者也踢掉）。
        """
        who = str(owner or "")
        with self._session() as session:
            deleted = session.execute(
                delete(HitlSweepLease)
                .where(
                    HitlSweepLease.name == SWEEP_LEASE_NAME,
                    HitlSweepLease.owner == who,
                )
                .execution_options(synchronize_session=False)
            )
            return deleted.rowcount == 1

    # -- 并发去重：行认领 ---------------------------------------------------

    def claim_expired(
        self,
        sidecar_request_id: str,
        *,
        cutoff_epoch: float,
        owner: str,
        ttl_s: float = DEFAULT_CLAIM_TTL_S,
        now: Optional[float] = None,
    ) -> bool:
        """认领一条到期 pending；``False`` = 这一行不归我（已被认领/已决/未到期）。

        这是**正确性**兜底，与租约分工不同：租约只保证"平时只有一个扫描者"，
        而租约可能被抢占或过期竞争（两个 worker 都认为自己持有）——行认领保证
        即使那种情况真的发生了，同一行也只被一个扫描者裁决。

        条件里刻意**不含** ``owner=me``：同一 owner 在 TTL 内**不许**重复认领。
        close 失败的行若能在下一轮立刻被自己重新认领，重算就会发生——那正是
        本次返工要消灭的重复计数（同一次裁决再 +1 ``rejected`` / 再打一次超时指标）。
        要让一行重新可裁决，只有两条路：显式 :meth:`release_claim`，或等 TTL 到期。
        """
        sid = str(sidecar_request_id or "")
        if not sid:
            raise ValueError("claim_expired requires sidecar_request_id")
        who = str(owner or "")
        if not who:
            raise ValueError("claim_expired requires a non-empty owner")
        at = float(now if now is not None else time.time())
        with self._session() as session:
            claimed = session.execute(
                update(HitlPendingApproval)
                .where(
                    HitlPendingApproval.sidecar_request_id == sid,
                    HitlPendingApproval.status == STATUS_PENDING,
                    HitlPendingApproval.created_at <= float(cutoff_epoch),
                    or_(
                        HitlPendingApproval.claim_owner == "",
                        HitlPendingApproval.claim_until <= at,
                    ),
                )
                .values(claim_owner=who, claim_until=at + max(0.0, float(ttl_s)))
                .execution_options(synchronize_session=False)
            )
            return claimed.rowcount == 1

    def release_claim(self, sidecar_request_id: str, *, owner: str) -> bool:
        """让出行认领（裁决未落地时调用）；``False`` = 这一行不由 ``owner`` 认领。

        只释放自己的认领（``WHERE claim_owner=me``）：别人接管后，慢速的旧持有者
        释放自己的那份是无效操作，绝不能顺手清掉新持有者的认领。
        条件里带 ``status=pending``：已闭合的行不需要让出（闭合时已清空认领列）。
        """
        sid = str(sidecar_request_id or "")
        if not sid:
            raise ValueError("release_claim requires sidecar_request_id")
        who = str(owner or "")
        if not who:
            raise ValueError("release_claim requires a non-empty owner")
        with self._session() as session:
            released = session.execute(
                update(HitlPendingApproval)
                .where(
                    HitlPendingApproval.sidecar_request_id == sid,
                    HitlPendingApproval.status == STATUS_PENDING,
                    HitlPendingApproval.claim_owner == who,
                )
                .values(claim_owner="", claim_until=0.0)
                .execution_options(synchronize_session=False)
            )
            return released.rowcount == 1

    # -- 写路径 -------------------------------------------------------------

    def save_pending(
        self,
        *,
        sidecar: Dict[str, Any],
        gate_request: Optional[Dict[str, Any]] = None,
        thread_id: str = "",
    ) -> None:
        """挂起落库（幂等 upsert，重置闭合列——同一 sidecar id 的重新挂起视为新一轮）。"""
        sid = str((sidecar or {}).get("request_id") or "")
        if not sid:
            raise ValueError("save_pending requires sidecar payload with request_id")
        with self._session() as session:
            row = session.get(HitlPendingApproval, sid)
            if row is None:
                unresolved = session.scalar(
                    select(func.count())
                    .select_from(HitlPendingApproval)
                    .where(HitlPendingApproval.status == STATUS_PENDING)
                )
                if (unresolved or 0) >= self.max_unresolved:
                    raise HitlPendingStoreFull(
                        f"pending_store_full: unresolved pending rows already at cap "
                        f"{self.max_unresolved}; new pending {sid!r} refused (显式拒绝，"
                        "不静默挤掉任何一条未决真源行)"
                    )
                row = HitlPendingApproval(sidecar_request_id=sid)
                session.add(row)
            row.gate_request_id = str((gate_request or {}).get("request_id") or "")
            row.thread_id = str(thread_id or "")
            row.tool_name = str(sidecar.get("tool_name", ""))
            row.declared_risk_level = str(sidecar.get("declared_risk_level", "L0"))
            row.status = STATUS_PENDING
            row.arguments = dict(sidecar.get("arguments") or {})
            row.evidence_ids = list(sidecar.get("evidence_ids") or ())
            row.artifact_refs = list(sidecar.get("artifact_refs") or ())
            row.gate_request = dict(gate_request) if gate_request else None
            row.created_at = float(sidecar.get("created_at") or time.time())
            row.decided_at = None
            row.decided_by = None
            row.decision_reason = None
            row.edited_arguments = None
            # 重新挂起 = 新生命周期：旧认领属于上一轮的扫描者，留着会让这一行
            # 在 TTL 内对所有人（含真正该扫它的那个）都不可裁决。
            row.claim_owner = ""
            row.claim_until = 0.0

    def close_decision(
        self,
        sidecar_request_id: str,
        *,
        status: str,
        decided_by: str = "",
        reason: str = "",
        edited_arguments: Optional[Dict[str, Any]] = None,
        sidecar: Optional[Dict[str, Any]] = None,
        gate_request: Optional[Dict[str, Any]] = None,
        thread_id: str = "",
    ) -> bool:
        """裁决闭合同一行；返回是否真的写入（``False`` = 行已决，一个字段都没改）。

        行不存在时**补写闭合行**（前提：调用方带上 ``sidecar`` 载荷）——库是真源，
        裁决必须留痕；"行没进过库所以不算数"等于给治理面开了第二条内存真源。
        桥侧只在内存有该请求时裁决，因此补写路径覆盖的是"pending 生于 flag 切换
        窗口"这类边缘态。

        **已决行不覆盖（CAS）**：``status != pending`` 时直接返回 ``False``，既不清
        认领列、也不写任何闭合列。**裁决只写一次**——跨实例竞态下（``--workers 2``
        时扫描租约只让一个 worker 扫，但"人工在另一个 worker 上点裁决"随时可能与
        到期拒同时发生）后手不得改写先手的 ``decided_by`` / ``decision_reason`` /
        ``status``：否则超时归因会被一次人工裁决抹掉，甚至把 ``rejected`` 改成
        ``approved``，让「已超时拒绝」这个治理事实凭空消失。
        """
        if status not in DECIDED_STATUSES:
            raise ValueError(
                f"invalid closing status {status!r}; must be one of "
                f"{sorted(DECIDED_STATUSES)}"
            )
        sid = str(sidecar_request_id or "")
        if not sid:
            raise ValueError("close_decision requires sidecar_request_id")
        with self._session() as session:
            row = session.get(HitlPendingApproval, sid)
            if row is None:
                if sidecar is None:
                    raise ValueError(
                        "closing an unseen pending requires the sidecar payload for upsert"
                    )
                row = HitlPendingApproval(sidecar_request_id=sid)
                session.add(row)
                row.gate_request_id = str((gate_request or {}).get("request_id") or "")
                row.thread_id = str(thread_id or "")
                row.tool_name = str(sidecar.get("tool_name", ""))
                row.declared_risk_level = str(sidecar.get("declared_risk_level", "L0"))
                row.arguments = dict(sidecar.get("arguments") or {})
                row.evidence_ids = list(sidecar.get("evidence_ids") or ())
                row.artifact_refs = list(sidecar.get("artifact_refs") or ())
                row.gate_request = dict(gate_request) if gate_request else None
                row.created_at = float(sidecar.get("created_at") or time.time())
            elif row.status != STATUS_PENDING:
                # 已决行：先手已给出终局归因，后手只许"没写成"，不许改写。
                return False
            row.status = status
            row.decided_at = time.time()
            row.decided_by = str(decided_by or "")
            row.decision_reason = str(reason or "")
            row.edited_arguments = dict(edited_arguments) if edited_arguments is not None else None
            # 闭合 = 这一轮认领结束（两条分支都在此汇合）：不清空的话认领者
            # 崩溃后租约列会一直挂着一个"过期前不可再认领"的残留在已决行上，
            # 排障时看着像"已决却还被占着"。
            row.claim_owner = ""
            row.claim_until = 0.0
            return True

    # -- 读路径 -------------------------------------------------------------

    def load(self, sidecar_request_id: str) -> Optional[Dict[str, Any]]:
        """按主键取一条（跨实例裁决的回捞入口）。不存在 ⇒ None（不抛：缺失是正常值域）。"""
        with self._session() as session:
            row = session.get(HitlPendingApproval, str(sidecar_request_id or ""))
            return self._to_record(row) if row is not None else None

    def statuses_for(self, sidecar_request_ids: Iterable[str]) -> Dict[str, str]:
        """按主键批量取 status（待审面过滤用）：只返回存在的行，缺失的 id 不出现。

        用途唯一：桥的 ``pending()`` 用它把"内存里还有、库行已闭合"的陈旧项剔出
        待审面。那是**跨实例裁决的常态**而不是异常——``--workers 2`` 下行由扫描
        租约只让一个 worker 扫，被拒掉的那条 pending 在另一个实例的 ``_requests``
        里没人清理，若不按库行 status 过滤，运维的审批列表会一直挂着一条已超时
        拒绝的行，还能对它再点一次裁决。

        只回"存在的行"是刻意的：库里**没有**该 id = 从未落库成功的 pending
        （``save_pending`` 失败的内存副本），必须继续列出，否则运维就看不到它了。
        因此调用方只能剔除"明确存在且非 pending"的项。
        """
        # 去空 + 去重 + 排序：参数顺序确定，SQL 指纹与排障日志才可复现
        ids = sorted({str(sid) for sid in sidecar_request_ids if sid})
        if not ids:
            # 空集合连库都不读（``_session()`` 会建连/建表）——一次空查询没有意义
            return {}
        with self._session() as session:
            rows = session.execute(
                select(HitlPendingApproval.sidecar_request_id, HitlPendingApproval.status).where(
                    HitlPendingApproval.sidecar_request_id.in_(ids)
                )
            ).all()
            return {str(sid): str(status) for sid, status in rows}

    def list_unresolved(self) -> List[Dict[str, Any]]:
        """全部未决行（启动期 restore 与跨实例待审列表用），按挂起时刻升序。"""
        with self._session() as session:
            rows = (
                session.scalars(
                    select(HitlPendingApproval)
                    .where(HitlPendingApproval.status == STATUS_PENDING)
                    .order_by(HitlPendingApproval.created_at)
                )
            ).all()
            return [self._to_record(r) for r in rows]

    def list_expired(self, cutoff_epoch: float, *, limit: int = 100) -> List[Dict[str, Any]]:
        """超过审批时限仍未决的行（T19 自动拒绝的扫描入口），按挂起时刻升序。

        判据只有两条：``status == pending``（已决行不得被二次裁决，fail-closed 语义
        不因超时而放宽）与 ``created_at <= cutoff_epoch``。与 :meth:`list_unresolved`
        同风格：只读、显式抛错、不在这层降级——读库失败如何被观察到由调用方决定
        （桥侧 ``expire_overdue`` fail-open 只留 warning，纪律 2）。

        ``limit`` 是**单次扫描上限**（不是"最多只拒绝这么多"）：扫描是周期任务，
        积压超过一屏时下一轮接着扫，避免一次把进程内的库会话和事件循环按住。

        **返回的行可能已被其他扫描者认领**：本方法只按 status/时间取数，不看
        ``claim_*`` 列（查询语义保持"到期就是到期"）。调用方必须先
        :meth:`claim_expired` 拿到认领才能裁决——否则两个扫描者会把同一行各拒一次。

        **已知边界（不改查询逻辑）**：升序 + ``limit`` 的组合在「积压超过一屏**且**
        最前面若干行反复处理失败」时会让后面的行等得更久——每一轮重试仍从**最老**
        的那条开始，失败行因此把屏占满，其余到期行排在它后面一直轮不到。选择保留
        这个形态的理由：换成「跳过失败行」需要记录失败次数或失败时刻（新列 + 新
        状态机），而 `expire_overdue` 的单行失败本该是**坏数据/闸门侧异常**这种
        罕见形态，靠运维看 warning 修数据即可；为罕见形态加一列状态，代价大于收益。
        失败可见性由桥侧的 warning 与 ``stats()`` 承担，不靠"静默跳过"。
        """
        with self._session() as session:
            rows = (
                session.scalars(
                    select(HitlPendingApproval)
                    .where(
                        HitlPendingApproval.status == STATUS_PENDING,
                        HitlPendingApproval.created_at <= float(cutoff_epoch),
                    )
                    .order_by(HitlPendingApproval.created_at)
                    .limit(max(1, int(limit)))
                )
            ).all()
            return [self._to_record(r) for r in rows]

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _to_record(row: HitlPendingApproval) -> Dict[str, Any]:
        """行 → 桥可用的记录（``sidecar`` 键刻意保持 ``to_public_dict()`` 的形状）。"""
        return {
            "sidecar_request_id": row.sidecar_request_id,
            "gate_request_id": row.gate_request_id,
            "thread_id": row.thread_id,
            "status": row.status,
            "created_at": row.created_at,
            "decided_at": row.decided_at,
            "decided_by": row.decided_by,
            "decision_reason": row.decision_reason,
            "edited_arguments": row.edited_arguments,
            "sidecar": {
                "request_id": row.sidecar_request_id,
                "tool_name": row.tool_name,
                "arguments": dict(row.arguments or {}),
                "declared_risk_level": row.declared_risk_level,
                "evidence_ids": list(row.evidence_ids or ()),
                "artifact_refs": list(row.artifact_refs or ()),
                "created_at": row.created_at,
            },
            "gate_request": dict(row.gate_request) if row.gate_request else None,
        }


def build_default_hitl_pending_store() -> HitlPendingStore:
    """缺省连接面：共享惰性 engine，构造时刻不建池、不连库、不建表。

    只在 ``HITL_PENDING_PERSIST_ENABLED=true`` 的装配路径被调用（判据唯一源：
    ``hitl_bridge._resolve_pending_store``）。
    """
    return HitlPendingStore(session_factory=None)
