"""P5（岗位对照·Proactive 5.1/5.2/5.3/5.4）：cron **执行** 半——tick 自愈四件套 + 工具 E2E + 接线。

对照 source-code-study §3.7（hermes ``cron/scheduler.py`` 四件套 + 交付回执）与
§5 5.4（``Cron*`` 工具族）。钉死契约：

1. **flag 治理**：``CRON_ENABLED`` 默认 False ⇒ 声明清单/执行器/后台循环
   三处零接线（与基线逐项一致）；flag-on ⇒ 4 工具进声明清单（title 契约）+
   handler 注册（幂等）。
2. **tick 次序契约**：① revision 不等 ⇒ ``yielded`` 直接返回（不让位则
   其余全跑）；② stale reap + 事故单；③ **先 ``advance_next_run`` 再
   runner**（runner 内可见已推进的 next_run_at——崩溃安全的前提）；
   ④ ``try_acquire`` 拿不到 ⇒ 跳过（防并发双跑）；⑤ 成功销案/失败开单
   （同因节流）、``CancelledError`` 透传但先 ``release``（账本停在受理 =
   unacked）、通知按账本重投（封顶 ``dead``）、``unacked`` 汇总。
3. **cron_run 工具路径**：``run_job_now`` 与 tick 同款簿记语义；缺 id /
   停用 / 在跑 ⇒ 诚实失败（failed ToolResult，不静默）。
4. **超时漂移守卫**：``cron_run`` spec ``timeout_ms`` 必须大于执行半
   ``CRON_JOB_TIMEOUT_S``（工具自身等待上限 < 任务超时 = 必然假失败）。
5. **接线**：main.py 组合根 runner（tool 型过完整闸门）+ startup 装配 +
   shutdown 停环；五处同步（settings/selfcheck/.env.example/configuration.md/p41）。
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

import pytest

from agentix.application.services import cron_scheduler as cs
from agentix.application.services.cron_jobs import (
    CRON_JOB_TIMEOUT_S,
    CronJob,
    JobStore,
    reset_job_store,
)
from agentix.application.services.cron_scheduler import (
    CronScheduler,
    install_cron_tools,
    start_cron_scheduler,
    stop_cron_scheduler,
)
from agentix.tools import registry_init
from agentix.tools.adapters.cron_tools import (
    build_cron_run_spec,
)
from agentix.tools.definitions import registry as tool_registry
from agentix.tools.gateway.executor import get_tool_executor, reset_tool_executor
from agentix.config import settings

# ---------------------------------------------------------------------------
# 基线断言：flag 默认关（flag 治理；见 configuration.md §5）
# ---------------------------------------------------------------------------

assert settings.CRON_ENABLED is False, "P5 基线：cron 调度默认关"
assert settings.CRON_TICK_SECONDS == 60, "P5 基线：tick 间隔默认 60s"

CRON_TOOLS = ("cron_create", "cron_list", "cron_delete", "cron_run")
TEST_TOOLS = CRON_TOOLS + ("t_p58_echo",)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _purge() -> None:
    registry_init._DECLARATIONS.clear()
    reset_tool_executor()
    reset_job_store()
    stop_cron_scheduler()
    for name in TEST_TOOLS:
        if tool_registry.has_tool(name):
            tool_registry.delete_tool(name)


@pytest.fixture(autouse=True)
def _clean():
    _purge()
    yield
    _purge()


def _store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs.json")


def _mk_scheduler(store: JobStore, runner, **kwargs) -> CronScheduler:
    kwargs.setdefault("revision_provider", lambda: "")
    return CronScheduler(runner=runner, store=store, **kwargs)


def _add_job(
    store: JobStore,
    *,
    now: float = 1_700_000_000.0,
    name: str = "job-1",
    expr: str = "* * * * *",
    deliver: bool = True,
) -> CronJob:
    job = CronJob.new(name=name, expr=expr, kind="agent", now=now, prompt="hi", deliver=deliver)
    return store.add_job(job)


def _make_due(store: JobStore, job_id: str, now: float) -> None:
    job = store.get_job(job_id)
    job.next_run_at = now - 1
    store.update_job(job)


class StubPort:
    """事件端口替身（投递成功/恒失败两种脚本）。"""

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.published: list[tuple[str, dict]] = []

    async def publish(self, event_type: str, payload: dict) -> None:
        if self.fail:
            raise RuntimeError("port down")
        self.published.append((event_type, payload))


def _echo_spec():
    from agentix.tools.definitions import ToolSpec

    return ToolSpec(name="t_p58_echo", risk_level="L0", description="echo test tool")


# ===========================================================================
# 1. flag 治理（声明清单 / 安装接线）
# ===========================================================================


class TestFlagGovernance:
    def test_flag_off_declarations_exclude_cron_tools(self):
        registry_init._DECLARATIONS.clear()
        names = [spec.name for spec, _ in registry_init.get_declarations()]
        assert not any(n.startswith("cron_") for n in names)

    def test_flag_off_install_registers_nothing(self, tmp_path):
        install_cron_tools(_mk_scheduler(_store(tmp_path), _ok_runner))  # flag 关：首行返回
        ex = get_tool_executor()
        assert not any(ex.has_handler(n) for n in CRON_TOOLS)
        assert not any(tool_registry.has_tool(n) for n in CRON_TOOLS)
        assert cs.get_cron_scheduler() is None

    def test_flag_on_declarations_title_contract_and_risk(self, monkeypatch):
        monkeypatch.setattr(settings, "CRON_ENABLED", True)
        registry_init._DECLARATIONS.clear()
        decls = {spec.name: cls for spec, cls in registry_init.get_declarations()}
        assert set(CRON_TOOLS) <= set(decls)
        # title 契约：schema 类名 == 工具名（selection._spec_for_schema 回查依赖）
        for name in CRON_TOOLS:
            assert decls[name].__name__ == name
        # 风险/副作用：配置级工具 L1 + side_effect（禁 auto-retry；审批半走 MEDIUM）
        for name in ("cron_create", "cron_delete", "cron_run"):
            spec = next(s for s, _ in registry_init.get_declarations() if s.name == name)
            assert spec.risk_level == "L1" and spec.side_effect is True
            assert "control" in spec.capabilities
        spec_list = next(s for s, _ in registry_init.get_declarations() if s.name == "cron_list")
        assert spec_list.risk_level == "L0" and spec_list.side_effect is False

    def test_cron_run_spec_timeout_exceeds_job_timeout(self):
        """漂移守卫：工具等待上限必须 > 执行半 per-job 超时（否则必然假失败）。"""
        assert build_cron_run_spec().timeout_ms > CRON_JOB_TIMEOUT_S * 1000

    def test_flag_on_install_registers_handlers_idempotent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "CRON_ENABLED", True)
        registry_init._DECLARATIONS.clear()
        sch = _mk_scheduler(_store(tmp_path), _ok_runner)
        install_cron_tools(sch)
        ex = get_tool_executor()
        assert all(ex.has_handler(n) for n in CRON_TOOLS)
        assert all(tool_registry.has_tool(n) for n in CRON_TOOLS)
        assert cs.get_cron_scheduler() is sch
        install_cron_tools(sch)  # 幂等：重复安装不炸
        assert all(ex.has_handler(n) for n in CRON_TOOLS)


async def _ok_runner(job: CronJob) -> str:
    return f"ran:{job.name}"


# ===========================================================================
# 2. tick 次序契约（让位 / 推进 / 簿记 / 成败 / 销案）
# ===========================================================================


class TestTickContract:
    @pytest.mark.asyncio
    async def test_yield_on_revision_change(self, tmp_path):
        store = _store(tmp_path)
        called = []

        async def runner(job):
            called.append(job.name)
            return "x"

        sch = CronScheduler(runner=runner, store=store, revision_provider=lambda: "v2",
                            boot_revision="v1")
        _add_job(store)
        rep = await sch.tick(1_700_000_100.0)
        assert rep.yielded is True
        assert rep.due == 0 and called == []  # 让位 ⇒ 什么都不跑

    @pytest.mark.asyncio
    async def test_no_yield_when_provider_stable(self, tmp_path):
        store = _store(tmp_path)
        sch = _mk_scheduler(store, _ok_runner)
        _add_job(store)
        rep = await sch.tick(1_700_000_100.0)
        assert rep.yielded is False
        assert rep.ran == ["job-1"]  # 空 revision 两侧相等 ⇒ 不误杀

    @pytest.mark.asyncio
    async def test_advance_visible_to_runner_before_execute(self, tmp_path):
        """③ 先推进后执行：runner 被调时 next_run_at 已是未来点（崩溃安全前提）。"""
        store = _store(tmp_path)
        seen: list[float] = []

        async def runner(job: CronJob) -> str:
            seen.append(job.next_run_at)
            return "ok"

        job = _add_job(store, expr="0 9 * * *")
        now = 1_700_000_100.0
        _make_due(store, job.id, now)
        await _mk_scheduler(store, runner).tick(now)
        assert seen == [job.next_run_at]
        assert seen[0] > now  # 已推进（严格晚于本次 tick 时刻）

    @pytest.mark.asyncio
    async def test_success_updates_status_and_runs(self, tmp_path):
        store = _store(tmp_path)
        job = _add_job(store)
        now = 1_700_000_100.0
        _make_due(store, job.id, now)
        rep = await _mk_scheduler(store, _ok_runner).tick(now)
        assert rep.due == 1 and rep.ran == ["job-1"]
        assert rep.incidents_opened == 0 and rep.unacked == 0
        got = store.get_job(job.id)
        assert got.last_status == "ok" and not got.is_running

    @pytest.mark.asyncio
    async def test_not_due_and_disabled_jobs_skipped(self, tmp_path):
        store = _store(tmp_path)
        future = _add_job(store, name="future")
        future.next_run_at = 9_999_999_999.0  # 显式远期（不依赖构造时刻）
        store.update_job(future)
        disabled = _add_job(store, name="disabled")
        disabled.enabled = False
        store.update_job(disabled)
        now = 1_700_000_100.0
        rep = await _mk_scheduler(store, _ok_runner).tick(now)
        assert rep.due == 0 and rep.ran == []
        assert store.get_job(future.id).last_run_at is None

    @pytest.mark.asyncio
    async def test_skip_job_already_running(self, tmp_path):
        """② inflight：非 stale 的 running ⇒ 跳过（不并发双跑）。"""
        store = _store(tmp_path)
        job = _add_job(store)
        now = 1_700_000_100.0
        _make_due(store, job.id, now)
        job.running_since = now - 1  # 在跑
        store.update_job(job)
        called = []

        async def runner(j):
            called.append(j.name)
            return "x"

        rep = await _mk_scheduler(store, runner).tick(now)
        assert rep.skipped_running == ["job-1"] and called == []
        assert rep.due == 1  # 到期了，只是拿不到锁

    @pytest.mark.asyncio
    async def test_failure_opens_incident_and_releases(self, tmp_path):
        store = _store(tmp_path)
        job = _add_job(store)
        now = 1_700_000_100.0
        _make_due(store, job.id, now)

        async def boom(j):
            raise RuntimeError("boom")

        rep = await _mk_scheduler(store, boom).tick(now)
        assert rep.incidents_opened == 1 and rep.ran == []
        got = store.get_job(job.id)
        assert got.last_status == "failed" and not got.is_running  # 失败也 release
        assert store.open_incidents()[0]["message"] == "RuntimeError: boom"
        assert store.list_runs()[0]["ok"] is False  # 失败有回执 ⇒ 非 unacked
        assert rep.unacked == 0

    @pytest.mark.asyncio
    async def test_incident_throttled_across_failing_ticks(self, tmp_path):
        store = _store(tmp_path)
        job = _add_job(store)
        now = 1_700_000_100.0

        async def boom(j):
            raise RuntimeError("boom")

        sch = _mk_scheduler(store, boom)
        _make_due(store, job.id, now)
        rep1 = await sch.tick(now)
        _make_due(store, job.id, now + 60)
        rep2 = await sch.tick(now + 60)
        assert rep1.incidents_opened == 1
        assert rep2.incidents_opened == 0  # 同因不再开单
        assert len(store.open_incidents()) == 1
        assert store.open_incidents()[0]["count"] == 2  # 只累加计数（重复告警节流）

    @pytest.mark.asyncio
    async def test_success_resolves_open_incident(self, tmp_path):
        store = _store(tmp_path)
        job = _add_job(store)
        now = 1_700_000_100.0

        async def boom(j):
            raise RuntimeError("boom")

        _make_due(store, job.id, now)
        await _mk_scheduler(store, boom).tick(now)
        assert store.open_incidents()
        _make_due(store, job.id, now + 60)
        rep = await _mk_scheduler(store, _ok_runner).tick(now + 60)
        assert rep.incidents_resolved == 1
        assert store.open_incidents() == []  # ③ 成功自动销案

    @pytest.mark.asyncio
    async def test_timeout_marks_failed_and_releases(self, tmp_path):
        store = _store(tmp_path)
        job = _add_job(store)
        now = 1_700_000_100.0
        _make_due(store, job.id, now)

        async def slow(j):
            await asyncio.sleep(30)
            return "never"

        rep = await _mk_scheduler(store, slow, timeout_s=0.05).tick(now)
        assert rep.incidents_opened == 1
        got = store.get_job(job.id)
        assert got.last_status == "failed" and not got.is_running
        assert "TimeoutError" in store.open_incidents()[0]["message"]

    @pytest.mark.asyncio
    async def test_cancel_releases_but_run_stays_unacked(self, tmp_path):
        """取消透传（不是失败）：先 release 恢复可调度，账本停在受理（诚实 unacked）。"""
        store = _store(tmp_path)
        job = _add_job(store)
        now = 1_700_000_100.0
        _make_due(store, job.id, now)
        started = asyncio.Event()

        async def hang(j):
            started.set()
            await asyncio.Event().wait()  # 永不结束

        task = asyncio.create_task(_mk_scheduler(store, hang).tick(now))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        results = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)  # 透传
        got = store.get_job(job.id)
        assert not got.is_running and got.last_status == "cancelled"
        assert len(store.pending_runs()) == 1  # 受理未回执
        assert store.list_runs()[0]["completed_at"] is None

    @pytest.mark.asyncio
    async def test_unacked_runs_reported_in_tick_report(self, tmp_path):
        store = _store(tmp_path)
        job = _add_job(store)
        store.accept_run("ghost", job.id, 1.0)  # 模拟崩溃残留：受理后无回执
        rep = await _mk_scheduler(store, _ok_runner).tick(1_700_000_100.0)
        assert rep.unacked == 1  # 诚实暴露，不自动补跑


# ===========================================================================
# 3. 交付账本重投（§5.3 回执）
# ===========================================================================


class TestNotificationDelivery:
    @pytest.mark.asyncio
    async def test_deliver_true_publishes_user_notification(self, tmp_path):
        store = _store(tmp_path)
        port = StubPort()
        job = _add_job(store, deliver=True)
        now = 1_700_000_100.0
        _make_due(store, job.id, now)
        sch = _mk_scheduler(store, _ok_runner, event_port=port)
        rep = await sch.tick(now)
        assert rep.notifications_delivered == 1
        assert len(port.published) == 1
        etype, payload = port.published[0]
        assert etype == "user_notification"
        assert payload["job_id"] == job.id and payload["ok"] is True
        assert store.list_runs()[0]["notify_status"] == "delivered"

    @pytest.mark.asyncio
    async def test_deliver_false_no_notification(self, tmp_path):
        store = _store(tmp_path)
        port = StubPort()
        job = _add_job(store, deliver=False)
        now = 1_700_000_100.0
        _make_due(store, job.id, now)
        rep = await _mk_scheduler(store, _ok_runner, event_port=port).tick(now)
        assert rep.notifications_delivered == 0
        assert port.published == []
        assert store.list_runs()[0]["notify_status"] == "none"

    @pytest.mark.asyncio
    async def test_failed_delivery_retries_then_dead(self, tmp_path):
        """重投封顶：3 次全败 ⇒ ``dead``；期间每轮 tick 都重试（账本兜底）。"""
        store = _store(tmp_path)
        port = StubPort(fail=True)
        # 年任务：只在首个 tick 到期一次，后续 tick 纯重投不重复执行
        job = _add_job(store, expr="0 0 1 1 *", deliver=True)
        now = 1_700_000_100.0
        _make_due(store, job.id, now)
        sch = _mk_scheduler(store, _ok_runner, event_port=port)
        rep1 = await sch.tick(now)  # 跑 + 第 1 次投递失败
        assert rep1.notifications_delivered == 0
        entry = store.list_runs()[0]
        assert entry["notify_status"] == "pending" and entry["notify_attempts"] == 1
        await sch.tick(now + 60)  # 第 2 次失败（任务不再到期，只重投）
        assert store.list_runs()[0]["notify_attempts"] == 2
        rep3 = await sch.tick(now + 120)  # 第 3 次失败 ⇒ dead
        entry = store.list_runs()[0]
        assert entry["notify_status"] == "dead"
        assert store.notify_pending() == []
        assert rep3.notifications_delivered == 0
        await sch.tick(now + 180)  # dead 后不再重投
        assert store.list_runs()[0]["notify_attempts"] == 3


# ===========================================================================
# 4. run_job_now（cron_run 工具执行路径）
# ===========================================================================


class TestRunJobNow:
    @pytest.mark.asyncio
    async def test_success_advances_and_reports(self, tmp_path):
        store = _store(tmp_path)
        job = _add_job(store, expr="0 9 * * *")
        now = 1_700_000_100.0
        _make_due(store, job.id, now)
        out = await _mk_scheduler(store, _ok_runner).run_job_now(job.id, now=now)
        assert out["job_id"] == job.id and out["ran"] is True
        assert out["status"] == "ok" and out["incidents_opened"] == 0
        assert out["next_run_at"] > "2023-11-15"  # cron_description ISO-8601 UTC
        assert store.get_job(job.id).next_run_at > now  # 与 tick 同款先推进

    @pytest.mark.asyncio
    async def test_missing_job_raises_keyerror(self, tmp_path):
        sch = _mk_scheduler(_store(tmp_path), _ok_runner)
        with pytest.raises(KeyError):
            await sch.run_job_now("nope")

    @pytest.mark.asyncio
    async def test_disabled_job_raises(self, tmp_path):
        store = _store(tmp_path)
        job = _add_job(store)
        job.enabled = False
        store.update_job(job)
        with pytest.raises(ValueError, match="disabled"):
            await _mk_scheduler(store, _ok_runner).run_job_now(job.id)

    @pytest.mark.asyncio
    async def test_already_running_raises(self, tmp_path):
        store = _store(tmp_path)
        job = _add_job(store)
        job.running_since = time.time()  # 真实当下在跑（未 stale）
        store.update_job(job)
        with pytest.raises(ValueError, match="running"):
            await _mk_scheduler(store, _ok_runner).run_job_now(job.id)


# ===========================================================================
# 5. cron_* 工具 E2E（经 ToolExecutor 治理链）
# ===========================================================================


def _flag_on(monkeypatch, tmp_path) -> tuple[CronScheduler, object]:
    monkeypatch.setattr(settings, "CRON_ENABLED", True)
    registry_init._DECLARATIONS.clear()
    sch = _mk_scheduler(_store(tmp_path), _ok_runner)
    install_cron_tools(sch)
    return sch, get_tool_executor()


class TestCronToolsE2E:
    @pytest.mark.asyncio
    async def test_create_list_run_delete_roundtrip(self, monkeypatch, tmp_path):
        _, ex = _flag_on(monkeypatch, tmp_path)
        created = await ex.execute(
            "cron_create",
            {"name": "daily-report", "expr": "0 9 * * 1-5", "prompt": "生成日报"},
        )
        assert created.success, created.error
        job_id = created.data["job_id"]
        assert created.data["next_run_at"] > "2023-11-15"

        listed = await ex.execute("cron_list", {})
        assert listed.success and listed.data["total"] == 1
        assert listed.data["jobs"][0]["job_id"] == job_id
        assert listed.data["jobs"][0]["kind"] == "agent"

        ran = await ex.execute("cron_run", {"job_id": job_id})
        assert ran.success, ran.error
        assert ran.data["status"] == "ok" and ran.data["ran"] is True

        deleted = await ex.execute("cron_delete", {"job_id": job_id})
        assert deleted.success and deleted.data["deleted"] is True
        assert (await ex.execute("cron_list", {})).data["total"] == 0

    @pytest.mark.asyncio
    async def test_create_validation_failures_are_honest(self, monkeypatch, tmp_path):
        _, ex = _flag_on(monkeypatch, tmp_path)
        cases = [
            # (args, 期望错误子串)
            ({"name": "x", "expr": "0 9 * * *", "prompt": "p", "tool": "web_search"},
             "mutually exclusive"),
            ({"name": "x", "expr": "0 9 * * *"}, "either prompt"),
            ({"name": "x", "expr": "not a cron", "prompt": "p"}, "cron"),
            ({"name": "x", "expr": "* * * * *", "tool": "no_such_tool"}, "unknown tool"),
            ({"name": "", "expr": "* * * * *", "prompt": "p"}, "name"),
            ({"name": "y" * 100, "expr": "* * * * *", "prompt": "p"}, "name"),
        ]
        for args, fragment in cases:
            result = await ex.execute("cron_create", args)
            assert not result.success, f"应当失败: {args}"
            assert fragment in result.error, f"{args} → {result.error}"

    @pytest.mark.asyncio
    async def test_create_rejects_oversize_prompt(self, monkeypatch, tmp_path):
        """prompt 长度校验（防无界 prompt 落盘）：> MAX_CRON_PROMPT_CHARS 拒绝。

        2026-10-09 清理批次补齐：MAX_CRON_PROMPT_CHARS 原本被 import 进
        install_cron_tools 却从未参与校验（spec description 声称 ≤4000 但
        Field 与处理器均无约束）——无界 prompt 可落盘 jobs.json。
        """
        _, ex = _flag_on(monkeypatch, tmp_path)
        from agentix.tools.adapters.cron_tools import MAX_CRON_PROMPT_CHARS

        oversize = await ex.execute(
            "cron_create",
            {"name": "big", "expr": "0 9 * * 1-5", "prompt": "p" * (MAX_CRON_PROMPT_CHARS + 1)},
        )
        assert not oversize.success
        assert str(MAX_CRON_PROMPT_CHARS) in oversize.error
        # 边界内（恰好 4000）应通过校验（落到后续流程；此处只断言不报 prompt 长度错误）
        boundary = await ex.execute(
            "cron_create",
            {"name": "edge", "expr": "0 9 * * 1-5", "prompt": "p" * MAX_CRON_PROMPT_CHARS},
        )
        assert boundary.success, boundary.error
        assert "prompt exceeds" not in (boundary.error or "")

    @pytest.mark.asyncio
    async def test_create_tool_kind_roundtrip(self, monkeypatch, tmp_path):
        _, ex = _flag_on(monkeypatch, tmp_path)
        tool_registry.register(_echo_spec(), lambda: None)
        get_tool_executor().register_handler("t_p58_echo", lambda args: {"echo": args})
        created = await ex.execute(
            "cron_create",
            {"name": "echo-job", "expr": "* * * * *", "tool": "t_p58_echo",
             "args": {"k": 1}, "deliver": False},
        )
        assert created.success, created.error
        listed = await ex.execute("cron_list", {})
        job = listed.data["jobs"][0]
        assert job["kind"] == "tool" and job["target"] == "t_p58_echo"
        ran = await ex.execute("cron_run", {"job_id": created.data["job_id"]})
        assert ran.success and ran.data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_delete_missing_job_fails_honestly(self, monkeypatch, tmp_path):
        _, ex = _flag_on(monkeypatch, tmp_path)
        result = await ex.execute("cron_delete", {"job_id": "nope"})
        assert not result.success
        assert "not found" in result.error or "KeyError" in result.error

    @pytest.mark.asyncio
    async def test_create_with_unserializable_args_fails(self, monkeypatch, tmp_path):
        _, ex = _flag_on(monkeypatch, tmp_path)
        tool_registry.register(_echo_spec(), lambda: None)
        get_tool_executor().register_handler("t_p58_echo", lambda args: {"echo": args})
        result = await ex.execute(
            "cron_create",
            {"name": "x", "expr": "* * * * *", "tool": "t_p58_echo", "args": {"bad": object()}},
        )
        assert not result.success  # jobs.json 落盘契约：入参必须 JSON 可序列化


# ===========================================================================
# 6. 后台循环接线（start / stop）
# ===========================================================================


class TestSchedulerLoop:
    @pytest.mark.asyncio
    async def test_start_flag_off_returns_none(self, tmp_path):
        sch = _mk_scheduler(_store(tmp_path), _ok_runner)
        assert start_cron_scheduler(sch) is None

    @pytest.mark.asyncio
    async def test_start_stop_and_idempotent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "CRON_ENABLED", True)
        sch = _mk_scheduler(_store(tmp_path), _ok_runner)
        task = start_cron_scheduler(sch, tick_seconds=3600)
        assert task is not None and not task.done()
        again = start_cron_scheduler(sch, tick_seconds=3600)
        assert again is task  # 幂等：不重复起环
        stop_cron_scheduler()
        assert cs._loop_task is None
        assert cs.get_cron_scheduler() is None
        results = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)


# ===========================================================================
# 7. 接线与五处同步（源级断言）
# ===========================================================================


class TestSourceWiring:
    def test_main_py_has_full_cron_wiring(self):
        src = (PROJECT_ROOT / "agentix" / "main.py").read_text(encoding="utf-8")
        assert "_build_cron_runner" in src  # 组合根 runner（分层：application 不碰 DB）
        assert "install_cron_tools(" in src and "start_cron_scheduler(" in src
        assert "stop_cron_scheduler(" in src  # _shutdown 停环
        assert 'getattr(settings, "CRON_ENABLED", False)' in src  # flag 门控装配

    def test_runner_builder_defines_both_job_kinds(self):
        src = (PROJECT_ROOT / "agentix" / "main.py").read_text(encoding="utf-8")
        builder = re.search(r"def _build_cron_runner.*?(?=\nasync def |\ndef )", src, re.S)
        assert builder, "main.py 找不到 _build_cron_runner"
        body = builder.group(0)
        assert 'job.kind == "tool"' in body  # tool 型过完整闸门
        assert "get_chat_service" in body and "SessionLocal" in body  # agent 型走 chat

    def test_env_example_documents_cron_knobs(self):
        text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        assert "CRON_ENABLED=false" in text
        assert "CRON_TICK_SECONDS=60" in text

    def test_configuration_md_documents_cron(self):
        text = (PROJECT_ROOT / "docs" / "architecture" / "configuration.md").read_text(
            encoding="utf-8"
        )
        assert "`CRON_ENABLED`" in text and "`CRON_TICK_SECONDS`" in text

    def test_selfcheck_ignores_missing_cron_env(self):
        from agentix.config.selfcheck import DEFAULT_IGNORED_MISSING

        assert "CRON_ENABLED" in DEFAULT_IGNORED_MISSING
        assert "CRON_TICK_SECONDS" in DEFAULT_IGNORED_MISSING

    def test_settings_defaults(self):
        assert settings.CRON_ENABLED is False
        assert settings.CRON_TICK_SECONDS == 60

    def test_cron_jobs_module_avoids_infrastructure_imports(self):
        """分层守则：存储/执行半的**模块头 import** 不得引用 infrastructure（R2）。"""
        for rel in (
            "agentix/application/services/cron_jobs.py",
            "agentix/application/services/cron_scheduler.py",
        ):
            lines = (PROJECT_ROOT / rel).read_text(encoding="utf-8").splitlines()
            import_lines = [
                line for line in lines
                if line.strip().startswith(("from ", "import "))
            ]
            offenders = [line for line in import_lines if "infrastructure" in line]
            assert not offenders, f"{rel} 模块头引用了 infrastructure: {offenders}"

    def test_no_raw_shell_in_job_kinds(self):
        """安全边界：任务只有 agent/tool 两型，不提供任意 shell 执行。"""
        src = (PROJECT_ROOT / "agentix" / "application" / "services" / "cron_jobs.py").read_text(
            encoding="utf-8"
        )
        assert 'kind: str = ""  # "agent" | "tool"' in src or '"agent" | "tool"' in src
        assert "subprocess" not in src and "shell=True" not in src
