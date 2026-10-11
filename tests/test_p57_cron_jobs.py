"""P5（岗位对照·Proactive 5.1/5.3）：cron **存储/计划** 半——解析器 + JobStore + 交付账本 + 事故单。

对照 source-code-study §3.7 与 §5 5.1/5.3（hermes ``cron/jobs.py`` 分半 +
durable delivery ledger）。钉死契约：

1. **解析器（零依赖，UTC）**：5 段字段/范围/步进/列表；dow 7≡0；dom/dow
   双受限时**标准 OR** 语义；``next_cron_time`` 严格晚于 ``after``（精确
   fire 点返回下一个）；表达式 366 天内无 fire ⇒ ``CronExprError``
   （fail-fast——排不出来的表达式拒绝入库，而不是静默永不执行）；同一输入
   输出确定（不读墙上时钟）。
2. **存储**：原子写 roundtrip（tmp + ``os.replace``，落盘 JSON 可解析）；
   损坏文件 ⇒ 改名 ``.corrupt`` 留证 + 空库 fail-open；**先推进
   ``next_run_at`` 再执行**（推进即落盘，执行期崩溃只丢这一轮不 tight-loop）；
   inflight 簿记（拿不到 = 在跑/停用）、stale 阈值 ``>=`` 边界、
   ``release`` 记状态；runs/incidents 双 LRU 有界。
3. **交付账本（§5.3）**：``accept_run``（受理）≠ ``complete_run``（回执）——
   受理未回执进 ``pending_runs()``（诚实暴露，**不自动补跑**）；通知状态机
   pending → delivered；重投至 ``MAX_DELIVERY_ATTEMPTS`` 次封顶转 ``dead``。
4. **事故单**：同 job 同 message 且 open ⇒ 只累加 ``count``（进行中同一故障
   不刷屏）；成功自动销案；销案后再失败 ⇒ 新单；消息截断 + LRU 有界。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from agentix.application.services.cron_jobs import (
    CRON_STALE_MS,
    MAX_DELIVERY_ATTEMPTS,
    CronExprError,
    CronJob,
    JobStore,
    cron_description,
    next_cron_time,
    parse_cron,
)
from agentix.config import settings

# ---------------------------------------------------------------------------
# 基线断言：flag 默认关（flag 治理；见 configuration.md §5）
# ---------------------------------------------------------------------------

assert settings.CRON_ENABLED is False, "P5 基线：cron 调度默认关"
assert settings.CRON_TICK_SECONDS == 60, "P5 基线：tick 间隔默认 60s"


def _ts(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


def _store(tmp_path, **kwargs) -> JobStore:
    return JobStore(tmp_path / "jobs.json", **kwargs)


def _add_job(store: JobStore, *, now: float = 1_700_000_000.0, **kwargs) -> CronJob:
    kwargs.setdefault("name", "job-1")
    kwargs.setdefault("expr", "0 9 * * 1-5")
    job = CronJob.new(kind="agent", now=now, prompt="hi", **kwargs)
    return store.add_job(job)


# ===========================================================================
# 1. cron 表达式解析器
# ===========================================================================


class TestParseCron:
    def test_star_matches_everything(self):
        mins, hours, dom, months, dow, dom_star, dow_star = parse_cron("* * * * *")
        assert mins == frozenset(range(60))
        assert hours == frozenset(range(24))
        assert dom == frozenset(range(1, 32))
        assert months == frozenset(range(1, 13))
        assert dow == frozenset(range(7))
        assert dom_star and dow_star

    def test_ranges_steps_and_lists(self):
        mins, hours, dom, _, dow, _, _ = parse_cron("5-10/2 0,12 1-15 * 1-5")
        assert mins == frozenset({5, 7, 9})
        assert hours == frozenset({0, 12})
        assert dom == frozenset(range(1, 16))
        assert dow == frozenset({1, 2, 3, 4, 5})

    def test_star_step_field(self):
        mins, *_ = parse_cron("*/15 * * * *")
        assert mins == frozenset({0, 15, 30, 45})

    def test_dow_seven_maps_to_zero(self):
        *_, dow, dom_star, dow_star = parse_cron("0 0 * * 7")
        assert dow == frozenset({0})
        assert dom_star and not dow_star
        *_, dow0, _, _ = parse_cron("0 0 * * 0")
        assert dow0 == frozenset({0})

    def test_dom_dow_star_flags(self):
        *_, dom_star, dow_star = parse_cron("0 0 1 * *")
        assert not dom_star and dow_star
        *_, dom_star, dow_star = parse_cron("0 0 * * 1")
        assert dom_star and not dow_star

    @pytest.mark.parametrize(
        "expr",
        [
            "* * * *",  # 4 段
            "* * * * * 1",  # 6 段
            "",  # 空
            "60 * * * *",  # 分越界
            "* 24 * * *",  # 时越界
            "* * 0 * *",  # dom 越界（1-31）
            "* * * 13 *",  # 月越界
            "* * * * 8",  # dow 越界（0-7）
            "*/0 * * * *",  # 步进 0
            "10-5 * * * *",  # 倒置区间
            "a * * * *",  # 非数字
            "1,,2 * * * *",  # 空列表项
            "1-10/xx * * * *",  # 步进非数字
        ],
    )
    def test_bad_expressions_rejected(self, expr):
        with pytest.raises(CronExprError):
            parse_cron(expr)

    def test_error_is_valueerror(self):
        """``CronExprError`` 是 ``ValueError``（executor 统一转 failed 的路径）。"""
        assert issubclass(CronExprError, ValueError)


class TestNextCronTime:
    def test_strictly_after_partial_minute(self):
        base = 1_700_000_000  # :13:20 —— 严格晚于 ⇒ 下一分钟整点
        fire = next_cron_time("* * * * *", base)
        assert fire == (base // 60 + 1) * 60
        assert fire > base

    def test_exact_fire_returns_next_occurrence(self):
        first = next_cron_time("0 9 * * *", 0)  # 1970-01-01 09:00 UTC（周四）
        assert first == 32400
        # 恰好站在 fire 点 ⇒ 严格晚于 ⇒ 次日 09:00
        assert next_cron_time("0 9 * * *", first) == first + 86400

    def test_utc_weekday_of_epoch(self):
        """1970-01-01 是周四 ⇒ ``1-5`` 命中（口径是 UTC 不是本地时区）。"""
        assert next_cron_time("0 9 * * 1-5", 0) == 32400

    def test_dow_only_skips_weekend(self):
        # 1970-01-01 周四 10:00 → 下一个周一 01-05 09:00
        assert next_cron_time("0 9 * * 1", _ts(1970, 1, 1, 10, 0)) == 4 * 86400 + 32400

    def test_dom_dow_or_semantics(self):
        """dom/dow 双受限 ⇒ 标准 cron OR：任一侧命中即 fire。"""
        expr = "0 0 1 * 1"  # 每月 1 号 **或** 周一
        # 2023-12-31（周日）→ 次日既是 1 号又是周一（2024-01-01）
        assert next_cron_time(expr, _ts(2023, 12, 31, 12, 0)) == _ts(2024, 1, 1, 0, 0)
        # 恰在 01-01 00:00 fire 点 → 严格晚于 → 下一个周一 01-08（纯 dow 命中）
        assert next_cron_time(expr, _ts(2024, 1, 1, 0, 0)) == _ts(2024, 1, 8, 0, 0)

    def test_dom_only_when_dow_star(self):
        # dom 受限、dow 是 *：只看 1 号，与星期无关
        assert next_cron_time("0 0 1 * *", _ts(2023, 12, 20)) == _ts(2024, 1, 1, 0, 0)

    def test_month_jump(self):
        assert next_cron_time("0 0 1 6 *", _ts(2024, 1, 2)) == _ts(2024, 6, 1, 0, 0)

    def test_unfireable_within_window_raises(self):
        with pytest.raises(CronExprError, match="366"):
            next_cron_time("0 0 30 2 *", 0)  # 2 月 30 日永不存在

    def test_deterministic_same_input_same_output(self):
        expr, t = "15 */6 * * 3", 1_700_000_000.0
        assert next_cron_time(expr, t) == next_cron_time(expr, t)

    def test_cron_description_is_utc_iso(self):
        text = cron_description(_ts(2024, 6, 1, 9, 0))
        assert "2024-06-01T09:00:00" in text and "+00:00" in text


# ===========================================================================
# 2. JobStore：持久化 / 推进 / 簿记
# ===========================================================================


class TestJobStorePersistence:
    def test_roundtrip(self, tmp_path):
        store = _store(tmp_path)
        now = 1_700_000_000.0
        job = _add_job(store, now=now, args={"x": 1, "nested": {"y": [1, 2]}})
        reloaded = JobStore(tmp_path / "jobs.json")
        got = reloaded.get_job(job.id)
        assert got.name == "job-1"
        assert got.expr == "0 9 * * 1-5"
        assert got.kind == "agent"
        assert got.args == {"x": 1, "nested": {"y": [1, 2]}}
        assert got.next_run_at == job.next_run_at

    def test_atomic_write_valid_json_no_tmp_left(self, tmp_path):
        store = _store(tmp_path)
        _add_job(store)
        path = tmp_path / "jobs.json"
        assert not (tmp_path / "jobs.json.tmp").exists()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["version"] == 1
        assert len(payload["jobs"]) == 1

    def test_corrupt_file_fail_open_with_corrupt_rename(self, tmp_path):
        path = tmp_path / "jobs.json"
        path.write_text("{ definitely not json", encoding="utf-8")
        store = JobStore(path)
        assert store.list_jobs() == []
        assert (tmp_path / "jobs.json.corrupt").exists()

    def test_future_version_fail_open(self, tmp_path):
        path = tmp_path / "jobs.json"
        path.write_text(json.dumps({"version": 99, "jobs": []}), encoding="utf-8")
        assert JobStore(path).list_jobs() == []

    def test_advance_persists_before_any_execution(self, tmp_path):
        """先推进后执行（④）：推进即落盘——即使随后崩溃也已不指向旧 fire 点。"""
        store = _store(tmp_path)
        job = _add_job(store, expr="* * * * *")
        later = job.next_run_at + 120
        store.advance_next_run(job.id, later)
        reloaded = JobStore(tmp_path / "jobs.json").get_job(job.id)
        assert reloaded.next_run_at == store.get_job(job.id).next_run_at
        assert reloaded.next_run_at > later

    def test_list_stable_order_by_created_at(self, tmp_path):
        store = _store(tmp_path)
        a = _add_job(store, now=100.0, name="a")
        b = _add_job(store, now=200.0, name="b")
        assert [j.name for j in store.list_jobs()] == ["a", "b"]
        assert a.created_at < b.created_at


class TestJobStoreInflightBookkeeping:
    def test_acquire_conflict_then_release_frees(self, tmp_path):
        store = _store(tmp_path)
        job = _add_job(store)
        run_id = store.try_acquire(job.id, 1000.0)
        assert run_id and store.get_job(job.id).is_running
        # 在跑（未 stale）⇒ 第二次拿不到（防并发双跑）
        assert store.try_acquire(job.id, 1001.0) is None
        store.release(job.id, status="ok", at=1010.0)
        got = store.get_job(job.id)
        assert not got.is_running and got.last_status == "ok" and got.last_run_at == 1010.0
        assert store.try_acquire(job.id, 1011.0)  # 释放后可再拿

    def test_acquire_refused_when_disabled(self, tmp_path):
        store = _store(tmp_path)
        job = _add_job(store)
        job.enabled = False
        assert store.try_acquire(job.id, 1000.0) is None

    def test_stale_threshold_boundary(self, tmp_path):
        """``age >= CRON_STALE_MS`` 才 reap：恰到阈值 reap、差 100ms 不 reap。"""
        store = _store(tmp_path)
        job = _add_job(store)
        now = 1_000_000.0
        job.running_since = now - CRON_STALE_MS / 1000
        assert store.reap_stale(now) == [job.id]
        got = store.get_job(job.id)
        assert not got.is_running and got.last_status == "stale"
        assert got.active_run_id is None

        job2 = _add_job(store, name="job-2")
        job2.running_since = now - (CRON_STALE_MS - 100) / 1000
        assert store.reap_stale(now) == []

    def test_reap_does_not_fabricate_completion(self, tmp_path):
        """reap 只清簿记：没有 complete 回执 ⇒ run 停在受理（unacked）。"""
        store = _store(tmp_path)
        job = _add_job(store)
        store.try_acquire(job.id, 0.0)
        store.accept_run("r1", job.id, 0.0)  # 受理后进程「崩了」
        store.reap_stale(CRON_STALE_MS / 1000 + 1)
        assert len(store.pending_runs()) == 1
        assert store.list_runs()[0]["completed_at"] is None


# ===========================================================================
# 3. 交付账本（§5.3）+ 事故单
# ===========================================================================


class TestDeliveryLedger:
    def test_accept_is_not_complete(self, tmp_path):
        store = _store(tmp_path)
        job = _add_job(store)
        store.accept_run("r1", job.id, 10.0)
        assert len(store.pending_runs()) == 1  # 受理未回执 = unacked
        store.complete_run("r1", ok=True, summary="done", at=11.0, notify=False)
        assert store.pending_runs() == []
        entry = store.list_runs()[0]
        assert entry["ok"] is True and entry["summary"] == "done"
        assert entry["notify_status"] == "none"

    def test_complete_with_notify_enters_pending(self, tmp_path):
        store = _store(tmp_path)
        job = _add_job(store)
        store.accept_run("r1", job.id, 10.0)
        store.complete_run("r1", ok=True, summary="s", at=11.0, notify=True)
        assert len(store.notify_pending()) == 1
        assert store.pending_runs() == []  # 回执已到 ⇒ 不再 unacked

    def test_notify_delivered_clears_pending(self, tmp_path):
        store = _store(tmp_path)
        job = _add_job(store)
        store.accept_run("r1", job.id, 10.0)
        store.complete_run("r1", ok=True, summary="", at=11.0, notify=True)
        store.mark_notify("r1", "delivered", 12.0)
        entry = store.list_runs()[0]
        assert entry["notify_status"] == "delivered"
        assert entry["delivered_at"] == 12.0
        assert store.notify_pending() == []

    def test_notify_attempts_cap_turns_dead(self, tmp_path):
        """重投封顶：第 MAX 次失败 ⇒ ``dead``（告警不静默丢，也不无限重试）。"""
        store = _store(tmp_path)
        job = _add_job(store)
        store.accept_run("r1", job.id, 10.0)
        store.complete_run("r1", ok=True, summary="", at=11.0, notify=True)
        for i in range(MAX_DELIVERY_ATTEMPTS - 1):
            attempts = store.mark_notify("r1", "attempt", 12.0 + i)
            assert attempts == i + 1
            assert store.list_runs()[0]["notify_status"] == "pending"
            assert store.notify_pending()  # 未封顶 ⇒ 下轮还会重投
        attempts = store.mark_notify("r1", "attempt", 99.0)
        assert attempts == MAX_DELIVERY_ATTEMPTS
        assert store.list_runs()[0]["notify_status"] == "dead"
        assert store.notify_pending() == []  # 封顶后不再重投

    def test_complete_unknown_run_raises(self, tmp_path):
        store = _store(tmp_path)
        with pytest.raises(KeyError):
            store.complete_run("nope", ok=True, summary="", at=0.0, notify=False)

    def test_runs_are_lru_bounded(self, tmp_path):
        store = _store(tmp_path, max_runs=3)
        job = _add_job(store)
        for i in range(4):
            store.accept_run(f"r{i}", job.id, float(i))
        runs = store.list_runs()
        assert len(runs) == 3
        assert runs[0]["run_id"] == "r1"  # 最早的被裁掉


class TestIncidents:
    def test_same_message_throttled_to_count(self, tmp_path):
        store = _store(tmp_path)
        job = _add_job(store)
        inc1, created1 = store.record_incident(job.id, "boom", 1.0)
        inc2, created2 = store.record_incident(job.id, "boom", 2.0)
        assert created1 is True and created2 is False
        assert inc2["id"] == inc1["id"]
        assert inc2["count"] == 2 and inc2["last_seen"] == 2.0
        assert len(store.open_incidents()) == 1

    def test_resolve_then_reopen_gets_new_incident(self, tmp_path):
        store = _store(tmp_path)
        job = _add_job(store)
        first, _ = store.record_incident(job.id, "boom", 1.0)
        assert store.resolve_incidents(job.id, 2.0) == 1
        assert store.open_incidents() == []
        second, created = store.record_incident(job.id, "boom", 3.0)
        assert created and second["id"] != first["id"]
        assert second["count"] == 1

    def test_message_truncated_and_incidents_bounded(self, tmp_path):
        store = _store(tmp_path, max_incidents=2)
        job_a = _add_job(store, name="a")
        job_b = _add_job(store, name="b")
        job_c = _add_job(store, name="c")
        long_msg = "x" * 600
        inc, _ = store.record_incident(job_a.id, long_msg, 1.0)
        assert len(inc["message"]) <= 500
        store.record_incident(job_b.id, "m2", 2.0)
        store.record_incident(job_c.id, "m3", 3.0)
        assert len(store.list_incidents()) == 2  # LRU：最早的掉出

    def test_incident_job_id_records(self, tmp_path):
        store = _store(tmp_path)
        job = _add_job(store)
        store.record_incident(job.id, "boom", 1.0)
        assert store.open_incidents()[0]["job_id"] == job.id


# ===========================================================================
# 4. CronJob 模型
# ===========================================================================


class TestCronJobModel:
    def test_new_computes_first_fire(self):
        now = _ts(2024, 6, 1, 8, 0)
        job = CronJob.new(name="n", expr="0 9 * * *", kind="agent", now=now, prompt="p")
        assert job.next_run_at == _ts(2024, 6, 1, 9, 0)
        assert job.id and len(job.id) == 12

    def test_new_rejects_bad_expr(self):
        with pytest.raises(CronExprError):
            CronJob.new(name="n", expr="nope", kind="agent", now=0.0)

    def test_from_dict_forward_compatible(self):
        """未知字段丢弃、缺省字段补默认——文件 schema 前向演进不炸。"""
        job = CronJob.from_dict(
            {"id": "x", "name": "n", "expr": "* * * * *", "kind": "agent", "future_field": 1}
        )
        assert job.enabled is True and job.args == {} and job.deliver is True

    def test_to_dict_roundtrip(self):
        job = CronJob.new(name="n", expr="* * * * *", kind="tool", now=1.0, tool="echo")
        assert CronJob.from_dict(job.to_dict()) == job

    def test_is_running_flag(self):
        job = CronJob.new(name="n", expr="* * * * *", kind="agent", now=1.0, prompt="p")
        assert not job.is_running
        job.running_since = 2.0
        assert job.is_running
