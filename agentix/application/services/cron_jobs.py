"""P5（岗位对照·Proactive 5.1）：cron job **存储/计划** 半。

借鉴 hermes ``cron/jobs.py``（存储与计划）+ ``cron/scheduler.py``（执行）的
**分半架构**（source-code-study §3.7、§5 5.1）：本文件只回答「有哪些任务、
下次何时跑、谁在跑、跑出了什么」，不执行任何任务——执行在
``cron_scheduler.py``。

包含四块：

1. **cron 表达式解析**（``next_cron_time``）：标准 5 段（分 时 日 月 周）、
   **UTC 口径**（确定性、无本机时区漂移）、支持 ``*`` / ``N`` / ``A-B`` /
   ``A-B/S`` / ``*/S`` / 逗号列表；dom/dow 双受限时按标准 cron **OR** 语义；
   严格晚于 ``after`` 的下一个触发点，366 天内无触发 ⇒ ``CronExprError``
   （fail-fast——排不出来的表达式拒绝入库，而不是静默永不执行）。
   **零第三方依赖**（requirements 无 cron 库——不假设库可用，自研最小子集）。
2. **任务存储** ``JobStore``：``data/cron/jobs.json`` 单文件（jobs + runs +
   incidents 三段），**原子写**（tmp + ``os.replace``）、进程内单例（事件循环
   单写者，无跨实例竞态）；文件损坏 ⇒ 改名 ``.corrupt`` 留证 + 空库存启动
   （fail-open：cron 坏文件不该拖垮整个应用启动）。
3. **自愈簿记**（对应源侧四件套之 ②④）：``advance_next_run`` **先推进再
   执行**（进程崩溃也不会 tight-loop）；``try_acquire``/``release`` inflight
   簿记；``reap_stale`` 卡死任务探测（``CRON_STALE_MS`` 之外的 running 视为
   崩溃残留，清簿记但**不**把 run 标记完成——受理≠完成，见账本）。
4. **交付账本 + 事故单**（源侧 ③ + §5.3 durable delivery ledger）：
   ``runs`` 段记录 **受理（accept）→ 完成（complete）** 两个时刻——stale/崩溃
   的 run 停在 accepted 即「受理未回执」，由 ``pending_runs()`` 诚实暴露、
   **不自动补跑**（自动重放有双副作用风险）；``notify`` 状态机
   pending→delivered/dead（``MAX_DELIVERY_ATTEMPTS`` 次重投封顶）。事故单按
   job+message 聚合（进行中同因失败只累加 count = 重复告警节流），成功即
   自动销案。
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple
from uuid import uuid4

from loguru import logger

__all__ = [
    "CronExprError",
    "CronJob",
    "JobStore",
    "next_cron_time",
    "parse_cron",
    "get_job_store",
    "reset_job_store",
    "DEFAULT_JOBS_PATH",
    "CRON_JOB_TIMEOUT_S",
    "CRON_STALE_MS",
    "MAX_RUN_RECORDS",
    "MAX_INCIDENTS",
    "MAX_DELIVERY_ATTEMPTS",
]

# ---------------------------------------------------------------------------
# 常量（配置面收敛：可调项走 settings.CRON_*，这里是不变量级上界）
# ---------------------------------------------------------------------------

#: 单任务执行超时（秒）——``cron_run`` 工具 timeout_ms 必须大于本值（p58 漂移守卫）。
CRON_JOB_TIMEOUT_S = 120

#: inflight 判定为崩溃残留的阈值（毫秒）：running 超过它 ⇒ reap + 事故单。
CRON_STALE_MS = 600_000

#: runs 账本保留条数（有界：只保留最近若干轮的受理/回执记录）。
MAX_RUN_RECORDS = 200

#: 事故单保留条数（有界 LRU）。
MAX_INCIDENTS = 100

#: 单条交付通知的最大重投次数，超过 ⇒ ``dead``（告警不再静默丢）。
MAX_DELIVERY_ATTEMPTS = 3

#: 任务文件默认路径（相对仓库根；与 ``data/neo4j/graph.json`` 等同约定）。
DEFAULT_JOBS_PATH = "data/cron/jobs.json"

#: 文件 schema 版本（前向兼容守卫：读到更高版本 ⇒ 空库 fail-open 启动）。
JOBS_SCHEMA_VERSION = 1


class CronExprError(ValueError):
    """cron 表达式非法 / 366 天内无触发点（入库前 fail-fast）。"""


# ---------------------------------------------------------------------------
# 1. cron 表达式解析（最小子集，零依赖，UTC）
# ---------------------------------------------------------------------------


def _parse_field(text: str, lo: int, hi: int, name: str) -> Tuple[FrozenSet[int], bool]:
    """解析单个 cron 字段 → (值集合, 是否为裸 ``*``)。

    支持：``*``、``N``、``A-B``、``A-B/S``、``*/S``、``N/S``（= N-hi/S）、
    逗号列表。范围/步进越界、非数字 ⇒ ``CronExprError``。
    """
    text = (text or "").strip()
    if not text:
        raise CronExprError(f"cron field {name!r} is empty")
    is_star = text == "*"
    values: set = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise CronExprError(f"cron field {name!r} has empty item in {text!r}")
        step = 1
        step_given = False
        body = part
        if "/" in part:
            body, step_s = part.split("/", 1)
            body = body.strip()
            try:
                step = int(step_s)
            except ValueError as exc:
                raise CronExprError(
                    f"cron field {name!r}: bad step in {part!r}"
                ) from exc
            if step < 1:
                raise CronExprError(f"cron field {name!r}: step must be >= 1 in {part!r}")
            step_given = True
        if body == "*":
            start, end = lo, hi
        elif "-" in body:
            a_s, b_s = body.split("-", 1)
            try:
                start, end = int(a_s), int(b_s)
            except ValueError as exc:
                raise CronExprError(
                    f"cron field {name!r}: bad range in {part!r}"
                ) from exc
            if start > end:
                raise CronExprError(
                    f"cron field {name!r}: inverted range in {part!r}"
                )
        else:
            try:
                start = end = int(body)
            except ValueError as exc:
                raise CronExprError(
                    f"cron field {name!r}: bad value {part!r}"
                ) from exc
            if step_given:  # `N/S` = 从 N 到上限步进 S（cronie 兼容）
                end = hi
        if start < lo or end > hi:
            raise CronExprError(
                f"cron field {name!r}: {part!r} out of range [{lo}, {hi}]"
            )
        values.update(range(start, end + 1, step))
    if not values:  # pragma: no cover - range 生成后理论不可达
        raise CronExprError(f"cron field {name!r} matched no values")
    return frozenset(values), is_star


def parse_cron(
    expr: str,
) -> Tuple[FrozenSet[int], FrozenSet[int], FrozenSet[int], FrozenSet[int], FrozenSet[int], bool, bool]:
    """解析 5 段表达式 → (分, 时, 日, 月, 周, dom 是否裸 *, dow 是否裸 *)。

    周字段 7 ≡ 0（Sunday）——归一后 dow ∈ 0-6。
    """
    parts = (expr or "").split()
    if len(parts) != 5:
        raise CronExprError(
            f"cron expression must have 5 fields (got {len(parts)}): {expr!r}"
        )
    minutes, _ = _parse_field(parts[0], 0, 59, "minute")
    hours, _ = _parse_field(parts[1], 0, 23, "hour")
    dom, dom_star = _parse_field(parts[2], 1, 31, "day-of-month")
    months, _ = _parse_field(parts[3], 1, 12, "month")
    dow_raw, dow_star = _parse_field(parts[4], 0, 7, "day-of-week")
    dow = frozenset(0 if d == 7 else d for d in dow_raw)
    return minutes, hours, dom, months, dow, dom_star, dow_star


def _day_matches(
    dt: datetime,
    dom: FrozenSet[int],
    dow: FrozenSet[int],
    dom_star: bool,
    dow_star: bool,
) -> bool:
    """标准 cron 的 dom/dow 组合语义：双受限 ⇒ OR；单受限看单侧。"""
    dom_ok = dt.day in dom
    cron_dow = (dt.weekday() + 1) % 7  # python Mon=0..Sun=6 → cron Sun=0..Sat=6
    dow_ok = cron_dow in dow
    if dom_star and dow_star:
        return True
    if dom_star:
        return dow_ok
    if dow_star:
        return dom_ok
    return dom_ok or dow_ok


def next_cron_time(expr: str, after: float) -> float:
    """表达式 ``expr`` 严格晚于 ``after`` 的下一个触发时刻（UTC epoch 秒）。

    候选从下一分钟边界起，按 日 → 时 → 分 层层跳跃（不做全量分钟扫描，
    表达式无论多稀疏都是亚毫秒级）；366 天窗口内无解 ⇒ ``CronExprError``
    （拒绝入库而非静默永不执行）。
    """
    minutes, hours, dom, months, dow, dom_star, dow_star = parse_cron(expr)
    t = (int(math.floor(after)) // 60 + 1) * 60  # 严格晚于 after 的分钟边界
    limit = int(math.floor(after)) + 366 * 86400
    while t <= limit:
        dt = datetime.fromtimestamp(t, tz=timezone.utc)
        if dt.month not in months:
            t = (t // 86400 + 1) * 86400
            continue
        if not _day_matches(dt, dom, dow, dom_star, dow_star):
            t = (t // 86400 + 1) * 86400
            continue
        if dt.hour not in hours:
            t = (t // 3600 + 1) * 3600
            continue
        if dt.minute not in minutes:
            t = (t // 60 + 1) * 60
            continue
        return float(t)
    raise CronExprError(f"cron expression {expr!r} has no fire time within 366 days")


def cron_description(next_run_at: float) -> str:
    """``next_run_at`` → ISO-8601 UTC 文本（列表/通知展示用，确定性）。"""
    return datetime.fromtimestamp(next_run_at, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 2. 任务模型
# ---------------------------------------------------------------------------


@dataclass
class CronJob:
    """一个定时任务（纯数据；执行语义在 scheduler 半）。"""

    id: str
    name: str
    expr: str
    kind: str  # "agent" | "tool"
    prompt: str = ""
    tool: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    deliver: bool = True
    enabled: bool = True
    created_at: float = 0.0
    updated_at: float = 0.0
    next_run_at: float = 0.0
    last_run_at: Optional[float] = None
    last_status: str = ""  # "ok" | "failed" | "cancelled" | "stale" | ""
    running_since: Optional[float] = None  # inflight 簿记（②）
    active_run_id: Optional[str] = None

    @classmethod
    def new(
        cls,
        *,
        name: str,
        expr: str,
        kind: str,
        now: float,
        prompt: str = "",
        tool: str = "",
        args: Optional[Dict[str, Any]] = None,
        deliver: bool = True,
    ) -> "CronJob":
        """构造并直接算出首个 ``next_run_at``（表达式非法 ⇒ ``CronExprError``）。"""
        return cls(
            id=uuid4().hex[:12],
            name=name,
            expr=expr,
            kind=kind,
            prompt=prompt,
            tool=tool,
            args=dict(args or {}),
            deliver=deliver,
            created_at=now,
            updated_at=now,
            next_run_at=next_cron_time(expr, now),
        )

    @property
    def is_running(self) -> bool:
        return self.running_since is not None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CronJob":
        known = {f for f in cls.__dataclass_fields__}  # 容忍未来新增字段
        return cls(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------------------
# 3. 存储：jobs + runs 账本 + incidents（单文件、原子写、进程内单例）
# ---------------------------------------------------------------------------


class JobStore:
    """``jobs.json`` 持久化存储（存储/计划半；**不执行任何任务**）。

    单进程事件循环内是唯一写者（tick 与 ``cron_*`` 工具共享
    ``get_job_store()`` 单例），内存态即真源，``_save`` 只做原子落盘——
    不做读后写（无 read-modify-write 竞态窗口）。
    """

    def __init__(
        self,
        path: Optional[os.PathLike | str] = None,
        *,
        max_runs: int = MAX_RUN_RECORDS,
        max_incidents: int = MAX_INCIDENTS,
    ) -> None:
        self._path = Path(path or DEFAULT_JOBS_PATH)
        self._max_runs = max(1, int(max_runs))
        self._max_incidents = max(1, int(max_incidents))
        self._jobs: Dict[str, CronJob] = {}
        self._runs: List[Dict[str, Any]] = []
        self._incidents: List[Dict[str, Any]] = []
        self._load()

    # -- 持久化 -----------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # fail-open：坏文件不拖垮应用启动——改名留证，空库继续
            logger.warning("cron jobs file corrupt ({}): {} — starting fresh", self._path, exc)
            try:
                self._path.rename(self._path.with_name(self._path.name + ".corrupt"))
            except OSError:  # pragma: no cover - 并发/权限下 rename 可能失败
                pass
            return
        version = int(raw.get("version", 1) or 1)
        if version > JOBS_SCHEMA_VERSION:
            logger.warning(
                "cron jobs file version {} > {} (written by newer code) — starting fresh",
                version, JOBS_SCHEMA_VERSION,
            )
            return
        for item in raw.get("jobs") or []:
            try:
                job = CronJob.from_dict(item)
            except TypeError as exc:  # 字段不符：跳过该条不拖垮整库
                logger.warning("cron job entry dropped ({}): {}", exc, item.get("id"))
                continue
            self._jobs[job.id] = job
        self._runs = list(raw.get("runs") or [])
        self._incidents = list(raw.get("incidents") or [])

    def _save(self) -> None:
        payload = {
            "version": JOBS_SCHEMA_VERSION,
            "jobs": [job.to_dict() for job in self._jobs.values()],
            "runs": self._runs,
            "incidents": self._incidents,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=None, default=str),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)  # 原子替换（Windows 同卷语义）

    # -- 任务 CRUD --------------------------------------------------------

    def add_job(self, job: CronJob) -> CronJob:
        self._jobs[job.id] = job
        self._save()
        return job

    def update_job(self, job: CronJob) -> CronJob:
        job.updated_at = max(job.updated_at, job.created_at)
        self._jobs[job.id] = job
        self._save()
        return job

    def get_job(self, job_id: str) -> CronJob:
        job = self._jobs.get(str(job_id))
        if job is None:
            raise KeyError(f"cron job {job_id!r} not found")
        return job

    def delete_job(self, job_id: str) -> CronJob:
        job = self.get_job(job_id)
        del self._jobs[job.id]
        self._save()
        return job

    def list_jobs(self) -> List[CronJob]:
        """全部任务，按创建时刻 + id 稳定排序（确定性输出）。"""
        return sorted(self._jobs.values(), key=lambda j: (j.created_at, j.id))

    # -- 自愈簿记（四件套 ②④）---------------------------------------------

    def advance_next_run(self, job_id: str, now: float) -> float:
        """**先推进再执行**（④）：按当前时刻重算 next_run_at 并**先落盘**。

        tick 随后才跑 runner——执行期崩溃也只会丢「这一轮」，不会 tight-loop。
        """
        job = self.get_job(job_id)
        job.next_run_at = next_cron_time(job.expr, now)
        job.updated_at = now
        self._save()
        return job.next_run_at

    def try_acquire(self, job_id: str, now: float) -> Optional[str]:
        """inflight 簿记（②）：拿不到 ⇒ 任务在跑 / 已停用（返回 None）。"""
        job = self.get_job(job_id)
        if not job.enabled:
            return None
        if job.running_since is not None and (now - job.running_since) * 1000 < CRON_STALE_MS:
            return None  # 非 stale：仍在跑 ⇒ 不并发双跑
        run_id = uuid4().hex[:12]
        job.running_since = now
        job.active_run_id = run_id
        self._save()
        return run_id

    def release(self, job_id: str, *, status: str, at: float) -> None:
        """解除 inflight 簿记（不删 run 账本——受理记录独立保留）。"""
        job = self.get_job(job_id)
        job.running_since = None
        job.active_run_id = None
        job.last_run_at = at
        job.last_status = status
        job.updated_at = at
        self._save()

    def reap_stale(self, now: float) -> List[str]:
        """卡死探测（②）：running 超 ``CRON_STALE_MS`` ⇒ 清簿记 + 记 stale。

        被 reap 的 run **不**标完成（受理≠完成——那轮 turn 永远没回执，
        停在 accepted 由 ``pending_runs()`` 暴露）。
        """
        reaped: List[str] = []
        for job in self._jobs.values():
            if job.running_since is None:
                continue
            if (now - job.running_since) * 1000 >= CRON_STALE_MS:
                job.running_since = None
                job.active_run_id = None
                job.last_run_at = now
                job.last_status = "stale"
                job.updated_at = now
                reaped.append(job.id)
        if reaped:
            self._save()
        return reaped

    # -- 交付账本（§5.3：受理 ≠ 完成，需回执）--------------------------------

    def accept_run(self, run_id: str, job_id: str, at: float) -> Dict[str, Any]:
        """受理一条执行（runner 启动前记账；完成与否由 ``complete_run`` 回执）。"""
        entry: Dict[str, Any] = {
            "run_id": run_id,
            "job_id": job_id,
            "accepted_at": at,
            "completed_at": None,
            "ok": None,
            "summary": "",
            "notify_status": "none",
            "notify_attempts": 0,
        }
        self._runs.append(entry)
        if len(self._runs) > self._max_runs:
            del self._runs[: len(self._runs) - self._max_runs]
        self._save()
        return entry

    def complete_run(
        self, run_id: str, *, ok: bool, summary: str, at: float, notify: bool
    ) -> Dict[str, Any]:
        """回执：run 终态落账；``notify=True`` ⇒ 通知进 ``pending`` 待投。"""
        entry = self._run_entry(run_id)
        entry["completed_at"] = at
        entry["ok"] = bool(ok)
        entry["summary"] = str(summary)[:2000]
        entry["notify_status"] = "pending" if notify else "none"
        self._save()
        return entry

    def mark_notify(self, run_id: str, status: str, at: float) -> int:
        """通知状态机推进：``delivered`` / ``attempt``（计数）/ ``dead``。

        返回当前 ``notify_attempts``（调用方用于封顶判定）。
        """
        entry = self._run_entry(run_id)
        if status == "delivered":
            entry["notify_status"] = "delivered"
            entry["delivered_at"] = at
        elif status == "attempt":
            entry["notify_attempts"] = int(entry.get("notify_attempts") or 0) + 1
            if entry["notify_attempts"] >= MAX_DELIVERY_ATTEMPTS:
                entry["notify_status"] = "dead"
            # 未封顶 ⇒ 保持 pending，下个 tick 重投
        else:  # pragma: no cover - 内部调用方只会传上面两种
            raise ValueError(f"unknown notify status {status!r}")
        self._save()
        return int(entry.get("notify_attempts") or 0)

    def pending_runs(self) -> List[Dict[str, Any]]:
        """**受理未回执**（stale/崩溃的 run 停在 accepted）——诚实暴露，不补跑。"""
        return [r for r in self._runs if r.get("completed_at") is None]

    def notify_pending(self) -> List[Dict[str, Any]]:
        """待投通知（pending 且未封顶）。"""
        return [
            r
            for r in self._runs
            if r.get("notify_status") == "pending"
            and int(r.get("notify_attempts") or 0) < MAX_DELIVERY_ATTEMPTS
        ]

    def list_runs(self) -> List[Dict[str, Any]]:
        return list(self._runs)

    def _run_entry(self, run_id: str) -> Dict[str, Any]:
        for entry in reversed(self._runs):
            if entry.get("run_id") == run_id:
                return entry
        raise KeyError(f"run {run_id!r} not found")

    # -- 事故单（③：建单 / 重复告警节流 / 自动销案）--------------------------

    def record_incident(self, job_id: str, message: str, at: float):
        """记一次失败（返回 ``(incident, is_new)``）。

        **节流**：同 job 同 message 且仍有 open 单 ⇒ 只累加 count/last_seen
        （不新开单——进行中的同一故障不刷屏）；成功销案后再次失败 ⇒ 新单。
        """
        text = str(message)[:500]
        for inc in self._incidents:
            if inc["job_id"] == job_id and inc["message"] == text and inc.get("open"):
                inc["count"] = int(inc.get("count") or 1) + 1
                inc["last_seen"] = at
                self._save()
                return inc, False
        incident = {
            "id": uuid4().hex[:12],
            "job_id": job_id,
            "message": text,
            "count": 1,
            "first_seen": at,
            "last_seen": at,
            "open": True,
        }
        self._incidents.append(incident)
        if len(self._incidents) > self._max_incidents:
            del self._incidents[: len(self._incidents) - self._max_incidents]
        self._save()
        return incident, True

    def resolve_incidents(self, job_id: str, at: float) -> int:
        """成功即自动销案（返回关闭数量）。"""
        closed = 0
        for inc in self._incidents:
            if inc["job_id"] == job_id and inc.get("open"):
                inc["open"] = False
                inc["resolved_at"] = at
                closed += 1
        if closed:
            self._save()
        return closed

    def open_incidents(self) -> List[Dict[str, Any]]:
        return [i for i in self._incidents if i.get("open")]

    def list_incidents(self) -> List[Dict[str, Any]]:
        return list(self._incidents)


# ---------------------------------------------------------------------------
# 进程级单例
# ---------------------------------------------------------------------------

_store: Optional[JobStore] = None


def get_job_store(path: Optional[os.PathLike | str] = None) -> JobStore:
    """进程级单例（测试可 ``reset_job_store`` 后用 tmp 路径重造）。"""
    global _store
    if _store is None:
        _store = JobStore(path)
    return _store


def reset_job_store() -> None:
    global _store
    _store = None
