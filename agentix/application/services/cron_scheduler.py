"""P5（岗位对照·Proactive 5.1/5.2/5.3）：cron **执行** 半（tick 调度 + 自愈四件套 + 投递）。

与 ``cron_jobs.py``（存储/计划）分半——对齐 hermes ``scheduler.py`` vs
``jobs.py``（source-code-study §3.7）。``CronScheduler.tick()`` 一轮做五件事，
次序即契约（p58 逐条钉死）：

1. **代码版本让位**（①）：``revision_provider()`` 与 scheduler 启动时的
   ``boot_revision`` 不一致 ⇒ 本轮 ``yielded=True`` 直接返回——旧进程不跑
   新代码的计划（滚动部署/多进程共存语义；单进程部署下 provider 默认读
   ``AGENTIX_CODE_REVISION`` env，未配置时两侧同为空串 ⇒ 机制静默待命，
   不会误杀正常 tick）；
2. **卡死探测**（②）：``reap_stale`` 清掉超 ``CRON_STALE_MS`` 的 inflight
   簿记 + 开事故单（run 账本停在 accepted——受理≠完成，见 cron_jobs）；
3. **先推进 next_run_at 再执行**（④）：``advance_next_run`` 先落盘——执行期
   崩溃只会丢这一轮，不会 tight-loop；
4. **inflight 簿记**（②）：``try_acquire`` 拿不到 ⇒ 跳过（不并发双跑）；
   执行 → ``accept_run`` 受理 → runner（per-job 超时 ``CRON_JOB_TIMEOUT_S``）
   → ``complete_run`` 回执；失败 ⇒ 事故单（同因节流）、成功 ⇒ 自动销案
   （③）；``asyncio.CancelledError`` 透传（取消不是失败）但**先 release**
   （重启即可按计划恢复，无需等 stale 窗口）；
5. **交付账本回执**（§5.3）：``deliver=True`` 的成功 run 通知进 pending，
   每轮 tick 重投 ``user_notification`` 事件直到 event port 确认（回执），
   ``MAX_DELIVERY_ATTEMPTS`` 次封顶转 dead；``pending_runs()``（受理未完成）
   汇总进 ``TickReport.unacked`` 诚实可见。

**装配**（``install_cron_tools`` / ``start_cron_scheduler``）与 plan_mode /
notify_bridge 同款：声明在 capabilities（flag 门控），handler 在此；后台
tick 循环由 ``main._startup`` 仅在 ``CRON_ENABLED=true`` 时启动、
``_shutdown`` 取消——**runner 由组合根注入**（agent 型要开 DB 会话、走
chat_service，属 composition root 职责；application 不 import infrastructure）。
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from loguru import logger

from agentix.application.services.cron_jobs import (
    CRON_JOB_TIMEOUT_S,
    MAX_DELIVERY_ATTEMPTS,
    CronJob,
    JobStore,
    cron_description,
    get_job_store,
)
from agentix.config.settings import settings

__all__ = [
    "CronScheduler",
    "TickReport",
    "install_cron_tools",
    "start_cron_scheduler",
    "stop_cron_scheduler",
    "get_cron_scheduler",
]

#: 任务执行体签名：跑一轮、返回摘要文本；失败抛异常（进事故单）。
CronRunner = Callable[[CronJob], Awaitable[str]]

#: 站内通知事件类型（与 notify_user 同一事件流——投递走既有通知管线）。
CRON_NOTIFY_EVENT_TYPE = "user_notification"


def _default_revision_provider() -> str:
    """默认代码版本源：``AGENTIX_CODE_REVISION`` env（部署侧注入）。

    单进程部署（进程重启即换代码）下两侧恒为空串 ⇒ 恒等 ⇒ 不让位；
    滚动部署/多进程共存时 ops 注入递增 revision ⇒ 旧进程让位。
    """
    return os.environ.get("AGENTIX_CODE_REVISION", "")


@dataclass
class TickReport:
    """单轮 tick 的结构化结果（测试/诊断只读）。"""

    yielded: bool = False
    due: int = 0
    ran: List[str] = field(default_factory=list)
    skipped_running: List[str] = field(default_factory=list)
    stale_reaped: List[str] = field(default_factory=list)
    incidents_opened: int = 0
    incidents_resolved: int = 0
    notifications_delivered: int = 0
    unacked: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "yielded": self.yielded,
            "due": self.due,
            "ran": list(self.ran),
            "skipped_running": list(self.skipped_running),
            "stale_reaped": list(self.stale_reaped),
            "incidents_opened": self.incidents_opened,
            "incidents_resolved": self.incidents_resolved,
            "notifications_delivered": self.notifications_delivered,
            "unacked": self.unacked,
        }


class CronScheduler:
    """cron 执行半（存储引用 + runner 注入；不自建线程/进程）。"""

    def __init__(
        self,
        *,
        runner: CronRunner,
        store: Optional[JobStore] = None,
        revision_provider: Optional[Callable[[], str]] = None,
        boot_revision: Optional[str] = None,
        event_port: Any = None,
        timeout_s: float = CRON_JOB_TIMEOUT_S,
    ) -> None:
        self._runner = runner
        self._store = store or get_job_store()
        self._provider = revision_provider or _default_revision_provider
        # boot revision 在构造时捕获（与 hermes gateway boot image 同语义）
        self._boot_revision = self._provider() if boot_revision is None else boot_revision
        self._event_port = event_port  # None = 惰性取全局 get_event_port()
        self._timeout_s = float(timeout_s)

    @property
    def store(self) -> JobStore:
        return self._store

    # -- 主循环 -----------------------------------------------------------

    async def tick(self, now: Optional[float] = None) -> TickReport:
        """执行一轮调度（次序即契约，见模块 docstring）。"""
        rep = TickReport()
        # ① 代码版本让位
        current = self._provider()
        if current != self._boot_revision:
            rep.yielded = True
            logger.warning(
                "cron tick yielded (boot_revision={!r}, current={!r})",
                self._boot_revision, current,
            )
            return rep

        now = time.time() if now is None else float(now)

        # ② 卡死探测（清簿记 + 事故单；run 账本停在 accepted）
        for job_id in self._store.reap_stale(now):
            name = self._store.get_job(job_id).name
            rep.stale_reaped.append(name)
            _, created = self._store.record_incident(
                job_id,
                f"runner stuck: no completion within stale window ({job_id})",
                now,
            )
            if created:
                rep.incidents_opened += 1
            logger.warning("cron job {} reaped as stale", name)
        # 到期任务：先推进（④）→ 簿记（②）→ 执行
        for job in self._store.list_jobs():
            if not job.enabled or job.next_run_at <= 0 or job.next_run_at > now:
                continue
            rep.due += 1
            self._store.advance_next_run(job.id, now)  # ④ 先落盘再执行
            run_id = self._store.try_acquire(job.id, now)
            if run_id is None:
                rep.skipped_running.append(job.name)
                continue
            await self._execute(job, run_id, now, rep)

        # ⑤ 回执汇总 + 通知重投
        rep.unacked = len(self._store.pending_runs())
        rep.notifications_delivered = await self._flush_notifications()
        return rep

    # -- 单次执行 ---------------------------------------------------------

    async def _execute(self, job: CronJob, run_id: str, now: float, rep: TickReport) -> None:
        """受理 → runner（超时）→ 回执 / 事故单。次序：受理先于执行。"""
        self._store.accept_run(run_id, job.id, now)  # 受理 ≠ 完成
        try:
            summary = await asyncio.wait_for(self._runner(job), timeout=self._timeout_s)
        except asyncio.CancelledError:
            # 取消透传（取消不是失败），但先释放簿记：重启即按计划恢复
            self._store.release(job.id, status="cancelled", at=time.time())
            raise
        except Exception as exc:  # noqa: BLE001 - 任务失败转账本/事故单（设计意图）
            at = time.time()
            text = f"{type(exc).__name__}: {exc}"
            self._store.complete_run(run_id, ok=False, summary=text, at=at, notify=False)
            self._store.release(job.id, status="failed", at=at)
            _, created = self._store.record_incident(job.id, text, at)
            if created:
                rep.incidents_opened += 1
            logger.warning("cron job {} failed: {}", job.name, text)
        else:
            at = time.time()
            self._store.complete_run(
                run_id, ok=True, summary=str(summary or ""), at=at, notify=job.deliver
            )
            self._store.release(job.id, status="ok", at=at)
            rep.incidents_resolved += self._store.resolve_incidents(job.id, at)
            rep.ran.append(job.name)

    async def _flush_notifications(self) -> int:
        """pending 通知重投（event port 确认 = 回执；封顶转 dead）。

        时间戳取真实当下（不复用 tick 起点 ``now``）——否则 delivered_at
        会早于 completed_at，账本时序自相矛盾。
        """
        delivered = 0
        for entry in self._store.notify_pending():
            payload = {
                "title": "定时任务完成",
                "job_id": entry["job_id"],
                "run_id": entry["run_id"],
                "ok": bool(entry.get("ok")),
                "summary": str(entry.get("summary") or "")[:500],
            }
            try:
                port = self._event_port
                if port is None:
                    from agentix.agent.events.port import get_event_port

                    port = get_event_port()
                await port.publish(CRON_NOTIFY_EVENT_TYPE, payload)
            except Exception as exc:  # noqa: BLE001 - 投递失败下轮重试（账本兜底）
                attempts = self._store.mark_notify(entry["run_id"], "attempt", time.time())
                logger.warning(
                    "cron notify delivery failed (attempt {}/{}): {}",
                    attempts, MAX_DELIVERY_ATTEMPTS, exc,
                )
            else:
                self._store.mark_notify(entry["run_id"], "delivered", time.time())
                delivered += 1
        return delivered

    # -- cron_run 工具路径 -------------------------------------------------

    async def run_job_now(self, job_id: str, now: Optional[float] = None) -> Dict[str, Any]:
        """立即执行一次（与 tick 同款簿记/推进/事故单语义）。"""
        job = self._store.get_job(job_id)  # 不存在 ⇒ KeyError ⇒ ToolResult 诚实失败
        if not job.enabled:
            raise ValueError(f"cron job {job_id!r} is disabled")
        now = time.time() if now is None else float(now)
        self._store.advance_next_run(job.id, now)
        run_id = self._store.try_acquire(job.id, now)
        if run_id is None:
            raise ValueError(f"cron job {job_id!r} is already running")
        rep = TickReport()
        await self._execute(job, run_id, now, rep)
        refreshed = self._store.get_job(job.id)
        return {
            "job_id": job.id,
            "name": job.name,
            "run_id": run_id,
            "status": refreshed.last_status,
            "ran": job.name in rep.ran,
            "next_run_at": cron_description(refreshed.next_run_at),
            "incidents_opened": rep.incidents_opened,
        }


# ---------------------------------------------------------------------------
# 装配（flag 门控；与 plan_mode / notify_bridge 同款）
# ---------------------------------------------------------------------------

_scheduler_singleton: Optional[CronScheduler] = None
_loop_task: Optional[asyncio.Task] = None


def get_cron_scheduler() -> Optional[CronScheduler]:
    return _scheduler_singleton


def install_cron_tools(scheduler: CronScheduler) -> None:
    """注册 ``cron_*`` 处理器（幂等；依赖 flag-on 声明清单已有 spec）。"""
    if not bool(getattr(settings, "CRON_ENABLED", False)):
        return  # belt-and-suspenders：调用方已判 flag，这里再判一次
    import json

    from agentix.tools.adapters.cron_tools import MAX_CRON_NAME_CHARS, MAX_CRON_PROMPT_CHARS
    from agentix.tools.definitions import registry as tool_registry
    from agentix.tools.gateway.executor import get_tool_executor
    from agentix.tools.registry_init import ensure_registered

    ensure_registered()  # spec 先入 registry（executor 执行点要求 spec 存在）
    store = scheduler.store

    def _now() -> float:
        return time.time()

    async def _create(args: dict) -> dict:
        name = str(args.get("name") or "").strip()
        if not name or len(name) > MAX_CRON_NAME_CHARS:
            raise ValueError(f"name is required (1..{MAX_CRON_NAME_CHARS} chars)")
        expr = str(args.get("expr") or "").strip()
        prompt = str(args.get("prompt") or "").strip()
        # prompt 长度校验（防无界 prompt 落盘 jobs.json；spec description 声称
        # ≤MAX_CRON_PROMPT_CHARS 但此前 Field 与处理器都没约束——本处补齐，
        # 与 name 校验同款语义）
        if len(prompt) > MAX_CRON_PROMPT_CHARS:
            raise ValueError(f"prompt exceeds {MAX_CRON_PROMPT_CHARS} chars")
        tool = str(args.get("tool") or "").strip()
        if prompt and tool:
            raise ValueError("prompt and tool are mutually exclusive (give exactly one)")
        if not prompt and not tool:
            raise ValueError("either prompt (agent job) or tool (tool job) is required")
        args_payload = dict(args.get("args") or {})
        if tool:
            if not tool_registry.has_tool(tool):
                raise ValueError(f"unknown tool {tool!r} (not registered)")
            # 入参必须 JSON 可序列化（jobs.json 落盘契约）；不可序列化 ⇒
            # TypeError 上抛 ⇒ executor 统一转 failed ToolResult
            json.dumps(args_payload, ensure_ascii=False)
        # CronJob.new 内含 next_cron_time：非法表达式 ⇒ CronExprError 诚实失败
        job = CronJob.new(
            name=name,
            expr=expr,
            kind="tool" if tool else "agent",
            now=_now(),
            prompt=prompt,
            tool=tool,
            args=args_payload,
            deliver=bool(args.get("deliver", True)),
        )
        store.add_job(job)
        return {
            "job_id": job.id,
            "name": job.name,
            "kind": job.kind,
            "expr": job.expr,
            "next_run_at": cron_description(job.next_run_at),
        }

    async def _list(args: dict) -> dict:
        return {
            "jobs": [
                {
                    "job_id": job.id,
                    "name": job.name,
                    "kind": job.kind,
                    "expr": job.expr,
                    "target": job.tool or job.prompt[:80],
                    "enabled": job.enabled,
                    "running": job.is_running,
                    "next_run_at": cron_description(job.next_run_at),
                    "last_run_at": job.last_run_at,
                    "last_status": job.last_status,
                }
                for job in store.list_jobs()
            ],
            "total": len(store.list_jobs()),
        }

    async def _delete(args: dict) -> dict:
        job = store.delete_job(str(args.get("job_id") or ""))
        return {
            "deleted": True,
            "job_id": job.id,
            "name": job.name,
            "expr": job.expr,
        }

    async def _run(args: dict) -> dict:
        return await scheduler.run_job_now(str(args.get("job_id") or ""))

    executor = get_tool_executor()
    executor.register_handler("cron_create", _create)
    executor.register_handler("cron_list", _list)
    executor.register_handler("cron_delete", _delete)
    executor.register_handler("cron_run", _run)
    global _scheduler_singleton
    _scheduler_singleton = scheduler


async def _tick_loop(scheduler: CronScheduler, interval_s: float) -> None:
    """后台 tick 循环：先睡后跑（启动期不抢跑），单轮异常不杀循环。"""
    while True:
        await asyncio.sleep(interval_s)
        try:
            report = await scheduler.tick()
            if report.ran or report.stale_reaped or report.incidents_opened:
                logger.info("cron tick: {}", report.to_dict())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 调度循环必须比单个任务活得久
            logger.error("cron tick failed (loop continues): {}", exc)


def start_cron_scheduler(
    scheduler: CronScheduler, *, tick_seconds: Optional[int] = None
) -> asyncio.Task:
    """启动后台 tick 循环（幂等；flag-off 返回 None 零副作用）。"""
    global _loop_task
    if not bool(getattr(settings, "CRON_ENABLED", False)):
        return None
    if _loop_task is not None and not _loop_task.done():
        return _loop_task  # 已在跑：幂等
    interval = max(1, int(tick_seconds if tick_seconds is not None else settings.CRON_TICK_SECONDS))
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - 启动路径总在事件循环内
        logger.warning("start_cron_scheduler called outside event loop")
        return None
    _loop_task = loop.create_task(_tick_loop(scheduler, float(interval)))
    logger.info("cron scheduler loop started (interval={}s)", interval)
    return _loop_task


def stop_cron_scheduler() -> None:
    """取消后台 tick 循环（``_shutdown`` 调用；任务执行中被取消 ⇒ 先 release）。"""
    global _loop_task, _scheduler_singleton
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None
    _scheduler_singleton = None
