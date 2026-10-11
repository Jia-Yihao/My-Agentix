"""Phase 20 — 事件点亮（EventPort 后端/webhook/生命周期挂载）+ 离线进化 runner。

运行：./.venv/Scripts/python.exe -m pytest tests/test_agent_phase20_events_evolution.py -q
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentix.agent.events import (
    InMemoryEventBus,
    JsonlEventBus,
    build_event_port,
    get_event_port,
    reset_event_port,
    wait_external_event,
)
from agentix.agent.evolution import (
    EvolutionCarrier,
    OfflineEvolutionLoop,
    OnlineLoopRecorder,
)
from agentix.config import settings
from agentix.runtime.state import AgentState, AgentStatus
from evals.evolution_run import (
    GATE_CHECKERS,
    aggregate_signals,
    evaluate_gates,
    main as evolution_main,
)
from evals.quality_gate import evaluate_metric


@pytest.fixture(autouse=True)
def _fresh_event_port(monkeypatch):
    monkeypatch.setattr(settings, "EVENT_PORT_BACKEND", "memory", raising=False)
    reset_event_port()
    yield
    reset_event_port()


def _light_app() -> FastAPI:
    """只挂 v1 路由的轻量 app（不进 lifespan——事件端点无需启动副作用）。"""
    from agentix.interfaces.http.v1 import api_router

    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    return app


# ---------------------------------------------------------------------------
# EventPort 后端选择（EVENT_PORT_BACKEND）
# ---------------------------------------------------------------------------


class TestEventPortBackend:
    def test_default_is_memory(self, monkeypatch):
        monkeypatch.setattr(settings, "EVENT_PORT_BACKEND", "memory", raising=False)
        assert isinstance(build_event_port(), InMemoryEventBus)

    def test_unknown_backend_falls_back_to_memory(self, monkeypatch):
        monkeypatch.setattr(settings, "EVENT_PORT_BACKEND", "redis?!", raising=False)
        assert isinstance(build_event_port(), InMemoryEventBus)

    def test_jsonl_backend_uses_log_path(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "EVENT_PORT_BACKEND", "jsonl", raising=False)
        log = tmp_path / "events.jsonl"
        monkeypatch.setattr("agentix.agent.events.port.DEFAULT_EVENT_LOG_PATH", log)
        port = build_event_port()
        assert isinstance(port, JsonlEventBus)
        assert port.path == log

    def test_singleton_get_and_reset(self, monkeypatch):
        monkeypatch.setattr(settings, "EVENT_PORT_BACKEND", "memory", raising=False)
        first = get_event_port()
        assert get_event_port() is first
        reset_event_port()
        second = get_event_port()
        assert second is not first
        reset_event_port()


# ---------------------------------------------------------------------------
# JsonlEventBus：落盘 + 重启恢复
# ---------------------------------------------------------------------------


class TestJsonlEventBus:
    def test_publish_wait_and_restart(self, tmp_path):
        path = tmp_path / "events.jsonl"

        async def producer():
            bus = JsonlEventBus(path)
            eid = await bus.publish("judge.approved", {"v": 1}, event_key="k1")
            await bus.publish("other.event", {"v": 2})
            got = await bus.wait("judge.approved", timeout_s=0.0)
            return eid, got

        eid, got = asyncio.run(producer())
        assert got is not None and got["event_id"] == eid

        lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
        assert [x["op"] for x in lines] == ["publish", "publish", "state"]

        async def restart():
            bus2 = JsonlEventBus(path)
            # 已 delivered 的不再重投
            re_delivered = await bus2.wait("judge.approved", timeout_s=0.0)
            # pending 的仍可等到
            pending = await bus2.wait("other.event", timeout_s=0.0)
            # 同 event_key 跨重启仍命中去重
            eid2 = await bus2.publish("judge.approved", {"v": 1}, event_key="k1")
            return re_delivered, pending, eid2

        re_delivered, pending, eid2 = asyncio.run(restart())
        assert re_delivered is None
        assert pending is not None and pending["payload"] == {"v": 2}
        assert eid2 == eid

    def test_corrupt_trailing_line_tolerated(self, tmp_path):
        path = tmp_path / "events.jsonl"
        valid = {
            "op": "publish",
            "event_id": "e1",
            "event_type": "t",
            "payload": {"a": 1},
            "dedup_key": {"event_type": "t", "event_key": ""},
            "occurred_at": 1.0,
            "received_at": 1.0,
            "status": "pending",
            "attempts": 0,
            "max_retries": 3,
            "last_error": "",
            "expires_at": None,
        }
        path.write_text(json.dumps(valid) + "\n{\"op\": \"pu", encoding="utf-8")

        bus = JsonlEventBus(path)
        got = asyncio.run(bus.wait("t", timeout_s=0.0))
        assert got is not None and got["event_id"] == "e1"

    def test_missing_file_is_fresh(self, tmp_path):
        bus = JsonlEventBus(tmp_path / "nope.jsonl")
        assert bus.pending_count() == 0
        assert asyncio.run(bus.wait("t", timeout_s=0.0)) is None


# ---------------------------------------------------------------------------
# webhook POST /api/v1/events
# ---------------------------------------------------------------------------


class TestEventsWebhook:
    def test_publish_accepted_and_consumable(self):
        client = TestClient(_light_app())
        resp = client.post(
            "/api/v1/events",
            json={"event_type": "judge.approved", "payload": {"case": "c1"}, "event_key": "k1"},
        )
        assert resp.status_code == 202
        event_id = resp.json()["event_id"]
        assert resp.json()["status"] == "accepted"

        port = get_event_port()
        got = asyncio.run(port.wait("judge.approved", timeout_s=0.0))
        assert got is not None and got["event_id"] == event_id
        assert got["payload"] == {"case": "c1"}

    def test_dedup_same_event_key_returns_same_id(self):
        client = TestClient(_light_app())
        body = {"event_type": "t", "payload": {}, "event_key": "same"}
        first = client.post("/api/v1/events", json=body)
        second = client.post("/api/v1/events", json=body)
        assert first.status_code == second.status_code == 202
        assert first.json()["event_id"] == second.json()["event_id"]

    def test_empty_event_type_rejected(self):
        client = TestClient(_light_app())
        resp = client.post("/api/v1/events", json={"event_type": "", "payload": {}})
        assert resp.status_code == 422

    def test_control_chars_in_event_type_rejected(self):
        client = TestClient(_light_app())
        resp = client.post("/api/v1/events", json={"event_type": "a\nb", "payload": {}})
        assert resp.status_code == 422

    def test_oversized_payload_rejected(self):
        client = TestClient(_light_app())
        resp = client.post(
            "/api/v1/events",
            json={"event_type": "big", "payload": {"blob": "x" * 9000}},
        )
        assert resp.status_code == 413

    def test_negative_ttl_rejected(self):
        client = TestClient(_light_app())
        resp = client.post(
            "/api/v1/events", json={"event_type": "t", "payload": {}, "ttl_s": -1.0}
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# WAITING_EXTERNAL_EVENT 生命周期挂载
# ---------------------------------------------------------------------------


class TestWaitExternalEvent:
    @pytest.mark.asyncio
    async def test_hit_returns_payload_and_restores_executing(self):
        port = InMemoryEventBus()
        await port.publish("e1", {"x": 1})
        state = AgentState(run_id="r1")
        state.status = AgentStatus.EXECUTING
        out = await wait_external_event(state, "e1", port=port)
        assert out is not None and out["payload"] == {"x": 1}
        assert state.status is AgentStatus.EXECUTING  # WAITING → EXECUTING 已回切

    @pytest.mark.asyncio
    async def test_timeout_returns_none_and_restores_executing(self):
        port = InMemoryEventBus()
        state = AgentState(run_id="r2")
        state.status = AgentStatus.EXECUTING
        out = await wait_external_event(state, "never", port=port)
        assert out is None
        assert state.status is AgentStatus.EXECUTING

    @pytest.mark.asyncio
    async def test_illegal_state_transition_is_best_effort(self):
        # CREATED 无出边到 WAITING——状态转移自吞，事件等待本身不受阻
        port = InMemoryEventBus()
        await port.publish("e1", {"x": 1})
        state = AgentState(run_id="r3")  # 默认 CREATED
        out = await wait_external_event(state, "e1", port=port)
        assert out is not None
        assert state.status is AgentStatus.CREATED


# ---------------------------------------------------------------------------
# 质量门纯函数（与 CI 同一真源）
# ---------------------------------------------------------------------------


class TestEvaluateMetric:
    def test_higher_is_better_pass_and_fail(self):
        ok = evaluate_metric(0.98, 1.0, max_regression=0.02)
        assert ok["ok"] is True and ok["verdict"] == "PASS" and ok["comparison"] == "≥"
        bad = evaluate_metric(0.97, 1.0, max_regression=0.02)
        assert bad["ok"] is False and bad["verdict"] == "FAIL"

    def test_lower_is_better(self):
        ok = evaluate_metric(0.14, 0.12, max_regression=0.2, direction="lower_is_better")
        assert ok["ok"] is True and ok["comparison"] == "≤"
        bad = evaluate_metric(0.15, 0.12, max_regression=0.2, direction="lower_is_better")
        assert bad["ok"] is False


# ---------------------------------------------------------------------------
# 离线进化 runner
# ---------------------------------------------------------------------------


def _make_pkg(root: Path, trace_id: str, steps: list) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    pkg = {"trace_id": trace_id, "steps": steps}
    target = root / f"{trace_id}.json"
    target.write_text(json.dumps(pkg, ensure_ascii=False), encoding="utf-8")
    return target


def _steps() -> list:
    return [
        {"step_index": 0, "kind": "tool", "name": "search", "status": "ok"},
        {"step_index": 1, "kind": "final", "name": "answer", "status": "ok"},
    ]


def _proposal_file(tmp_path: Path, **overrides) -> Path:
    data = {"carrier": "prompt", "summary": "tune", "payload": {"text": "x"}, "touched_paths": []}
    data.update(overrides)
    target = tmp_path / "proposal.json"
    target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return target


class TestSignalsAggregation:
    def test_replays_into_recorder_aggregate(self, tmp_path):
        signals = tmp_path / "signals.jsonl"
        lines = [
            {"trace_id": "t1", "success": True, "cost_usd": 0.5},
            {"trace_id": "t2", "success": False, "first_error_stage": "tool", "cost_usd": 1.5},
        ]
        signals.write_text(
            "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8"
        )
        agg = aggregate_signals(str(signals))
        assert agg == {
            "n": 2,
            "success_rate": 0.5,
            "mean_cost_usd": 1.0,
            "error_stages": {"tool": 1},
        }

    def test_missing_or_absent_returns_none(self, tmp_path):
        assert aggregate_signals(None) is None
        assert aggregate_signals(str(tmp_path / "nope.jsonl")) is None


class TestEvolutionRunner:
    def test_happy_path_promotes(self, tmp_path, capsys):
        _make_pkg(tmp_path / "base", "t1", _steps())
        _make_pkg(tmp_path / "cand", "t1", _steps())
        (tmp_path / "qbase.json").write_text(
            json.dumps({"citation_accuracy": 0.9}), encoding="utf-8"
        )
        (tmp_path / "qcur.json").write_text(
            json.dumps({"citation_accuracy": 0.93}), encoding="utf-8"
        )
        signals = tmp_path / "signals.jsonl"
        signals.write_text(
            json.dumps({"trace_id": "t1", "success": True}) + "\n", encoding="utf-8"
        )
        code = evolution_main(
            [
                "--proposal", str(_proposal_file(tmp_path)),
                "--signals", str(signals),
                "--prefix-baseline", str(tmp_path / "base"),
                "--prefix-candidate", str(tmp_path / "cand"),
                "--quality-baseline", str(tmp_path / "qbase.json"),
                "--quality-current", str(tmp_path / "qcur.json"),
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert code == 0
        assert out["promoted"] is True
        assert out["staged"] is True
        assert out["signals"]["n"] == 1
        assert out["gates"]["golden_prefix"]["status"] == "pass"
        assert out["gates"]["citation"]["status"] == "pass"
        assert out["canary_pct"] == 10.0

    def test_missing_prefix_baseline_fails_closed(self, tmp_path, capsys):
        # Phase 17 纪律：真机基线缺失 → not_enforced ≠ 通过（拒绝伪造）
        (tmp_path / "qbase.json").write_text(json.dumps({"citation_accuracy": 0.9}), encoding="utf-8")
        (tmp_path / "qcur.json").write_text(json.dumps({"citation_accuracy": 0.93}), encoding="utf-8")
        code = evolution_main(
            [
                "--proposal", str(_proposal_file(tmp_path)),
                "--quality-baseline", str(tmp_path / "qbase.json"),
                "--quality-current", str(tmp_path / "qcur.json"),
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert code == 1
        assert out["promoted"] is False
        assert out["rejected_reason"] == "regression_failed"
        assert out["gates"]["golden_prefix"]["status"] == "not_enforced"

    def test_citation_regression_rejects(self, tmp_path, capsys):
        _make_pkg(tmp_path / "base", "t1", _steps())
        _make_pkg(tmp_path / "cand", "t1", _steps())
        (tmp_path / "qbase.json").write_text(json.dumps({"citation_accuracy": 0.9}), encoding="utf-8")
        (tmp_path / "qcur.json").write_text(json.dumps({"citation_accuracy": 0.85}), encoding="utf-8")
        code = evolution_main(
            [
                "--proposal", str(_proposal_file(tmp_path)),
                "--prefix-baseline", str(tmp_path / "base"),
                "--prefix-candidate", str(tmp_path / "cand"),
                "--quality-baseline", str(tmp_path / "qbase.json"),
                "--quality-current", str(tmp_path / "qcur.json"),
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert code == 1
        assert out["gates"]["citation"]["status"] == "fail"
        assert out["promoted"] is False

    def test_diverged_prefix_rejects(self, tmp_path, capsys):
        _make_pkg(tmp_path / "base", "t1", _steps())
        _make_pkg(
            tmp_path / "cand", "t1",
            [{"step_index": 0, "kind": "tool", "name": "other", "status": "ok"}],
        )
        (tmp_path / "qbase.json").write_text(json.dumps({"citation_accuracy": 0.9}), encoding="utf-8")
        (tmp_path / "qcur.json").write_text(json.dumps({"citation_accuracy": 0.9}), encoding="utf-8")
        code = evolution_main(
            [
                "--proposal", str(_proposal_file(tmp_path)),
                "--prefix-baseline", str(tmp_path / "base"),
                "--prefix-candidate", str(tmp_path / "cand"),
                "--quality-baseline", str(tmp_path / "qbase.json"),
                "--quality-current", str(tmp_path / "qcur.json"),
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert code == 1
        assert out["gates"]["golden_prefix"]["status"] == "fail"

    def test_protected_verifier_path_rejected_at_propose(self, tmp_path, capsys):
        code = evolution_main(
            ["--proposal", str(_proposal_file(
                tmp_path,
                touched_paths=["agentix/policies/verifier/layered.py"],
            ))]
        )
        out = json.loads(capsys.readouterr().out)
        assert code == 1
        assert out["staged"] is False
        assert out["rejected_reason"] == "verifier_logic_protected"
        assert out["promoted"] is False

    def test_unwired_gates_fail_closed(self, tmp_path, capsys):
        # 非 prompt 载体的门尚无接入检查 → not_wired → 不晋升（fail-closed）
        code = evolution_main(
            ["--proposal", str(_proposal_file(tmp_path, carrier="tool_policy"))]
        )
        out = json.loads(capsys.readouterr().out)
        assert code == 1
        assert out["rejected_reason"] == "regression_failed"
        assert {g["status"] for g in out["gates"].values()} == {"not_wired"}

    def test_unknown_carrier_is_input_error(self, tmp_path, capsys):
        code = evolution_main(
            ["--proposal", str(_proposal_file(tmp_path, carrier="nope"))]
        )
        assert code == 2

    def test_malformed_proposal_is_input_error(self, tmp_path, capsys):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        code = evolution_main(["--proposal", str(bad)])
        assert code == 2


class TestGateAlignment:
    """Phase 20 第 4 项：进化门与 evals 同名检查对齐（钉死映射）。"""

    def test_prompt_gates_are_wired(self):
        loop = OfflineEvolutionLoop()
        prompt_gates = set(loop.regression_gate[EvolutionCarrier.PROMPT])
        assert prompt_gates == {"golden_prefix", "citation"}
        assert prompt_gates <= set(GATE_CHECKERS)

    def test_other_carrier_gates_report_not_wired(self, tmp_path, capsys):
        loop = OfflineEvolutionLoop()
        args = None  # not_wired 不需要参数——直接构造求值
        for carrier in (
            EvolutionCarrier.TOOL_POLICY,
            EvolutionCarrier.MEMORY_CONSOLIDATION,
            EvolutionCarrier.VERIFIER_META,
        ):
            gates = loop.regression_gate[carrier]
            assert not (set(gates) & set(GATE_CHECKERS))
        results = evaluate_gates(("risk_levels",), args)
        assert results["risk_levels"]["status"] == "not_wired"


class TestOnlineRecorderZeroWrite:
    """三防线之在线侧：record 面零写能力（Phase 20 强化断言）。"""

    def test_recorder_has_no_write_capability(self):
        rec = OnlineLoopRecorder()
        assert rec.write_capability is False
        assert not any(
            name.startswith(("apply", "patch", "write", "mutate"))
            for name in dir(rec)
            if callable(getattr(rec, name, None)) and not name.startswith("_")
        )


# ---------------------------------------------------------------------------
# 审查修复回归（2026-09-26 排查出的 4 个 bug）
# ---------------------------------------------------------------------------


class TestRunnerRobustness:
    def test_bad_signal_lines_skipped_not_crash(self, tmp_path):
        """缺必填字段 / cost_usd 非数的坏行跳过——runner 不得 TypeError/ValueError 崩。"""
        signals = tmp_path / "signals.jsonl"
        lines = [
            {"trace_id": "t1"},  # 缺 success
            {"trace_id": "t2", "success": True, "cost_usd": "not-a-number"},
            {"trace_id": "t3", "success": True, "cost_usd": 1.5},
        ]
        signals.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
        agg = aggregate_signals(str(signals))
        assert agg is not None
        assert agg["n"] == 1
        assert agg["mean_cost_usd"] == 1.5

    def test_empty_prefix_baseline_fails_closed(self, tmp_path, capsys):
        """空基线目录 ≠ 真基线——不得空真通过（Phase 17 拒伪造纪律）。"""
        (tmp_path / "base").mkdir()
        _make_pkg(tmp_path / "cand", "t1", _steps())
        (tmp_path / "qbase.json").write_text(json.dumps({"citation_accuracy": 0.9}), encoding="utf-8")
        (tmp_path / "qcur.json").write_text(json.dumps({"citation_accuracy": 0.9}), encoding="utf-8")
        code = evolution_main(
            [
                "--proposal", str(_proposal_file(tmp_path)),
                "--prefix-baseline", str(tmp_path / "base"),
                "--prefix-candidate", str(tmp_path / "cand"),
                "--quality-baseline", str(tmp_path / "qbase.json"),
                "--quality-current", str(tmp_path / "qcur.json"),
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert code == 1
        assert out["gates"]["golden_prefix"]["status"] == "not_enforced"
        assert out["promoted"] is False

    def test_non_numeric_baseline_citation_not_enforced(self, tmp_path, capsys):
        (tmp_path / "qbase.json").write_text(
            json.dumps({"citation_accuracy": "oops"}), encoding="utf-8"
        )
        (tmp_path / "qcur.json").write_text(json.dumps({"citation_accuracy": 0.9}), encoding="utf-8")
        code = evolution_main(
            [
                "--proposal", str(_proposal_file(tmp_path)),
                "--quality-baseline", str(tmp_path / "qbase.json"),
                "--quality-current", str(tmp_path / "qcur.json"),
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert code == 1
        assert out["gates"]["citation"]["status"] == "not_enforced"

    def test_non_numeric_current_citation_fails(self, tmp_path, capsys):
        (tmp_path / "qbase.json").write_text(json.dumps({"citation_accuracy": 0.9}), encoding="utf-8")
        (tmp_path / "qcur.json").write_text(
            json.dumps({"citation_accuracy": "oops"}), encoding="utf-8"
        )
        code = evolution_main(
            [
                "--proposal", str(_proposal_file(tmp_path)),
                "--quality-baseline", str(tmp_path / "qbase.json"),
                "--quality-current", str(tmp_path / "qcur.json"),
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert code == 1
        assert out["gates"]["citation"]["status"] == "fail"


class TestEventKeyBounded:
    def test_oversized_event_key_rejected(self):
        """event_key 会进事件日志——必须有界，绕过 payload 上限的大键 → 422。"""
        client = TestClient(_light_app())
        resp = client.post(
            "/api/v1/events",
            json={"event_type": "t", "payload": {}, "event_key": "x" * 300},
        )
        assert resp.status_code == 422

    def test_normal_event_key_accepted(self):
        client = TestClient(_light_app())
        resp = client.post(
            "/api/v1/events",
            json={"event_type": "t", "payload": {}, "event_key": "k" * 64},
        )
        assert resp.status_code == 202
