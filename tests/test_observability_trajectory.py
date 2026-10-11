"""可观测性补齐（F1 完整回放 / F6 步骤级轨迹 / F3 Pass^k / F7 回流）测试。

全部离线，不依赖 Redis / LLM / Docker。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from agentix.application.services.trajectory import (
    InMemoryTrajectorySink,
    TrajectoryRecorder,
    TrajectoryStep,
    locate_first_error,
    redact_and_truncate,
    reset_trajectory_recorder,
)


# ---------------------------------------------------------------------------
# F6：步骤级轨迹
# ---------------------------------------------------------------------------


class TestTrajectoryRecorder:
    def setup_method(self):
        self.rec = TrajectoryRecorder()

    def test_records_in_order_with_incrementing_index(self):
        self.rec.record(kind="llm", name="router", trace_id="t1")
        self.rec.record(kind="tool", name="cypher_query", trace_id="t1")
        self.rec.record(kind="llm", name="general", trace_id="t1")
        steps = self.rec.get("t1")
        assert [s.step_index for s in steps] == [0, 1, 2]
        assert [s.name for s in steps] == ["router", "cypher_query", "general"]

    def test_trace_isolation(self):
        self.rec.record(kind="llm", name="a", trace_id="t1")
        self.rec.record(kind="llm", name="b", trace_id="t2")
        assert [s.name for s in self.rec.get("t1")] == ["a"]
        assert [s.name for s in self.rec.get("t2")] == ["b"]

    def test_first_error_locates_earliest_failure(self):
        self.rec.record(kind="llm", name="router", status="ok", trace_id="t")
        self.rec.record(kind="tool", name="milvus", status="error",
                        error="conn refused", trace_id="t")
        self.rec.record(kind="llm", name="finalize", status="error",
                        error="later", trace_id="t")
        first = self.rec.first_error("t")
        assert first is not None
        assert first.name == "milvus"
        assert first.step_index == 1

    def test_first_error_none_when_all_ok(self):
        self.rec.record(kind="llm", name="a", status="ok", trace_id="t")
        assert self.rec.first_error("t") is None

    def test_replay_bundle_shape(self):
        self.rec.record(kind="llm", name="router", status="ok", trace_id="t")
        self.rec.record(kind="error", name="retrieval_failed", status="error",
                        error="boom", trace_id="t")
        bundle = self.rec.replay("t")
        assert bundle["trace_id"] == "t"
        assert bundle["step_count"] == 2
        assert bundle["first_error_step_index"] == 1
        assert bundle["first_error"]["name"] == "retrieval_failed"
        assert len(bundle["steps"]) == 2
        # 可 JSON 序列化（可落盘回放）
        assert json.loads(json.dumps(bundle))["step_count"] == 2

    def test_reset_clears(self):
        self.rec.record(kind="llm", name="a", trace_id="t")
        self.rec.reset()
        assert self.rec.get("t") == []

    def test_record_never_raises_on_broken_sink(self):
        class BoomSink:
            def append(self, trace_id, step):
                raise RuntimeError("sink down")

            def list(self, trace_id):
                return []

        rec = TrajectoryRecorder(sink=BoomSink())
        assert rec.record(kind="llm", name="a", trace_id="t") is None  # 不抛


class TestTrajectorySinkBounds:
    def test_step_count_bounded(self):
        sink = InMemoryTrajectorySink(max_steps_per_trace=3)
        rec = TrajectoryRecorder(sink=sink)
        for i in range(10):
            rec.record(kind="llm", name=f"n{i}", trace_id="t")
        steps = rec.get("t")
        assert len(steps) == 3
        assert [s.name for s in steps] == ["n7", "n8", "n9"], "保留最新，丢弃最早"

    def test_trace_count_bounded(self):
        sink = InMemoryTrajectorySink(max_traces=2)
        rec = TrajectoryRecorder(sink=sink)
        for i in range(5):
            rec.record(kind="llm", name="x", trace_id=f"t{i}")
        assert rec.get("t0") == []
        assert len(rec.get("t4")) == 1


class TestLocateFirstError:
    def test_pure_function_ignores_order(self):
        steps = [
            TrajectoryStep(step_index=2, kind="llm", name="c", status="error"),
            TrajectoryStep(step_index=0, kind="llm", name="a", status="ok"),
            TrajectoryStep(step_index=1, kind="llm", name="b", status="ok"),
        ]
        first = locate_first_error(steps)
        assert first is not None and first.name == "c" and first.step_index == 2

    def test_empty_returns_none(self):
        assert locate_first_error([]) is None


# ---------------------------------------------------------------------------
# F1：脱敏 + 截断
# ---------------------------------------------------------------------------


class TestRedactAndTruncate:
    def test_truncates_long_text(self):
        out = redact_and_truncate("x" * 1000)
        assert len(out) <= 500 + len("…[truncated]")
        assert out.endswith("…[truncated]")

    def test_masks_sk_key(self):
        out = redact_and_truncate("key=sk-abcdef1234567890 end")
        assert "sk-abcdef1234567890" not in out
        assert "***" in out

    def test_masks_api_key_assignment(self):
        out = redact_and_truncate("api_key: supersecretvalue")
        assert "supersecretvalue" not in out

    def test_masks_bearer_token(self):
        out = redact_and_truncate("Authorization: Bearer tok123456")
        assert "tok123456" not in out

    def test_non_string_input(self):
        assert "1" in redact_and_truncate({"a": 1})

    def test_short_text_unchanged(self):
        assert redact_and_truncate("民法典第577条") == "民法典第577条"


# ---------------------------------------------------------------------------
# F1：LLM 调用写入轨迹（含 payload 捕获开关）
# ---------------------------------------------------------------------------


class TestLLMTrajectoryWiring:
    def setup_method(self):
        reset_trajectory_recorder()

    def test_records_llm_step_without_payload_by_default(self, monkeypatch):
        from agentix.application.agents.llm_factory import _record_llm_trajectory
        from agentix.application.services.trajectory import get_trajectory_recorder

        _record_llm_trajectory(
            role="router",
            model="m",
            ok=True,
            err=None,
            latency_ms=12.0,
            prompt_tokens=3,
            completion_tokens=4,
            call_args=({"secret": "x"},),
            call_kwargs={},
            response=None,
        )
        steps = get_trajectory_recorder().get("-")
        assert len(steps) == 1
        assert steps[0].kind == "llm"
        assert steps[0].name == "router"
        assert steps[0].status == "ok"
        # 默认不捕获 payload（隐私保护）
        assert "prompt_preview" not in steps[0].detail

    def test_captures_redacted_payload_when_enabled(self, monkeypatch):
        from agentix.application.agents.llm_factory import _record_llm_trajectory
        from agentix.application.services.trajectory import get_trajectory_recorder
        from agentix.config import settings

        monkeypatch.setattr(
            settings, "OBSERVABILITY_PAYLOAD_CAPTURE", True, raising=False
        )

        class _Resp:
            content = "回答：民法典第577条  sk-abcdef1234567890"

        _record_llm_trajectory(
            role="general",
            model="m",
            ok=True,
            err=None,
            latency_ms=1.0,
            prompt_tokens=1,
            completion_tokens=1,
            call_args=("问题：api_key: leakme",),
            call_kwargs={},
            response=_Resp(),
        )
        detail = get_trajectory_recorder().get("-")[0].detail
        assert "prompt_preview" in detail and "response_preview" in detail
        assert "leakme" not in detail["prompt_preview"]
        assert "sk-abcdef1234567890" not in detail["response_preview"]

    def test_failure_records_error_status(self, monkeypatch):
        from agentix.application.agents.llm_factory import _record_llm_trajectory
        from agentix.application.services.trajectory import get_trajectory_recorder

        _record_llm_trajectory(
            role="summarizer",
            model="m",
            ok=False,
            err="TimeoutError: boom",
            latency_ms=99.0,
            prompt_tokens=0,
            completion_tokens=0,
            call_args=(),
            call_kwargs={},
            response=None,
        )
        step = get_trajectory_recorder().get("-")[0]
        assert step.status == "error"
        assert "boom" in (step.error or "")


# ---------------------------------------------------------------------------
# F3：Pass^k
# ---------------------------------------------------------------------------


def _golden(gid: str, route: str, *, sources=None, reject=False) -> Dict[str, Any]:
    return {
        "id": gid,
        "question": f"q-{gid}",
        "route_type": route,
        "expected_sources": sources or [],
        "expect_reject": reject,
    }


def _pred(gid: str, route: str, *, sources=None, status="ok", message="") -> Dict[str, Any]:
    return {
        "id": gid,
        "route_type": route,
        "sources": sources or [],
        "status": status,
        "message": message,
    }


class TestPassK:
    def test_all_runs_pass(self):
        from evals.run_eval import compute_pass_k

        golden = [_golden("1", "kb-query"), _golden("2", "general-query")]
        preds = [
            _pred("1", "kb-query"),
            _pred("1", "kb-query"),
            _pred("2", "general-query"),
            _pred("2", "general-query"),
        ]
        m = compute_pass_k(golden, preds, k=2)
        assert m["pass_k"] == 1.0
        assert m["pass_at_k"] == 1.0
        assert m["mean_task_success"] == 1.0
        assert m["pass_k_max_runs_observed"] == 2

    def test_flaky_task_fails_pass_k_but_passes_at_k(self):
        """关键语义：pass^k 要求**全部**成功；pass@k 只需至少一次。"""
        from evals.run_eval import compute_pass_k

        golden = [_golden("1", "kb-query")]
        preds = [
            _pred("1", "kb-query"),          # ok
            _pred("1", "general-query"),     # wrong route
        ]
        m = compute_pass_k(golden, preds, k=2)
        assert m["pass_k"] == 0.0, "有一次失败 → pass^k 为 0"
        assert m["pass_at_k"] == 1.0, "有一次成功 → pass@k 为 1"
        assert m["mean_task_success"] == 0.5

    def test_k_limits_runs_per_task(self):
        from evals.run_eval import compute_pass_k

        golden = [_golden("1", "kb-query")]
        preds = [
            _pred("1", "kb-query"),
            _pred("1", "kb-query"),
            _pred("1", "wrong"),
        ]
        assert compute_pass_k(golden, preds, k=2)["pass_k"] == 1.0
        assert compute_pass_k(golden, preds, k=3)["pass_k"] == 0.0

    def test_tasks_without_predictions_excluded_from_denominator(self):
        from evals.run_eval import compute_pass_k

        golden = [_golden("1", "kb-query"), _golden("2", "kb-query")]
        preds = [_pred("1", "kb-query")]
        m = compute_pass_k(golden, preds)
        assert m["pass_k_tasks"] == 1, "无预测任务不计入分母"
        assert m["pass_k"] == 1.0

    def test_citation_miss_counts_as_failure(self):
        from evals.run_eval import compute_pass_k

        golden = [_golden("1", "kb-query", sources=["民法典第577条"])]
        preds = [_pred("1", "kb-query", sources=[])]
        assert compute_pass_k(golden, preds)["pass_k"] == 0.0

    def test_reject_misclassification_counts_as_failure(self):
        from evals.run_eval import compute_pass_k

        golden = [_golden("1", "general-query", reject=True)]
        preds = [_pred("1", "general-query", message="民法典第1条内容如下…")]
        assert compute_pass_k(golden, preds)["pass_k"] == 0.0

    def test_summary_includes_pass_k_fields(self):
        from evals.run_eval import evaluate

        golden = [_golden("1", "kb-query")]
        preds = [_pred("1", "kb-query"), _pred("1", "kb-query")]
        summary = evaluate(golden, preds, k=2)["summary"]
        for key in (
            "pass_k",
            "pass_at_k",
            "mean_task_success",
            "pass_k_tasks",
            "pass_k_requested",
            "pass_k_max_runs_observed",
            "pass_k_tasks_with_full_runs",
        ):
            assert key in summary, f"缺少 {key}"

    def test_format_summary_line_shows_pass_k_when_repeated(self):
        from evals.run_eval import evaluate, format_summary_line

        golden = [_golden("1", "kb-query")]
        preds = [_pred("1", "kb-query"), _pred("1", "kb-query")]
        line = format_summary_line(evaluate(golden, preds, k=2)["summary"])
        assert "Pass^2" in line

    def test_format_summary_line_omits_pass_k_for_single_run(self):
        from evals.run_eval import evaluate, format_summary_line

        golden = [_golden("1", "kb-query")]
        line = format_summary_line(evaluate(golden, [_pred("1", "kb-query")])["summary"])
        assert "Pass^" not in line


# ---------------------------------------------------------------------------
# F7：失败样本回流（绝不自动改 golden）
# ---------------------------------------------------------------------------


class TestFeedbackIngest:
    def test_classify_failure_rules(self):
        from evals.feedback_ingest import classify_failure

        assert classify_failure({"status": "error"})[0] == "status_failure"
        assert classify_failure({"status": "degraded"})[0] == "status_failure"
        assert classify_failure({"error_code": "retrieval_failed"})[0] == "error_code"
        assert classify_failure({})[0] == "missing_route"
        assert classify_failure({"route_type": "kb-query", "message": "ok"}) is None

    def test_extract_candidates_ids_and_review_flag(self):
        from evals.feedback_ingest import extract_candidates

        records = [
            {"question": "q1", "status": "error", "trace_id": "t1"},
            {"question": "q2", "route_type": "kb-query", "message": "fine"},
            {"question": "q3", "error_code": "llm_timeout", "trace_id": "t3"},
        ]
        candidates = extract_candidates(records, start_index=5)
        assert [c["id"] for c in candidates] == ["C005", "C006"]
        assert all(c["_needs_review"] is True for c in candidates)
        assert candidates[0]["_source"]["failure_kind"] == "status_failure"
        assert candidates[1]["_source"]["failure_kind"] == "error_code"
        # 期望字段刻意留空，需人工校准（不得自证）
        assert all(c["expected_sources"] == [] for c in candidates)
        assert all(c["expect_reject"] is False for c in candidates)

    def test_run_never_modifies_golden(self, tmp_path: Path):
        from evals.feedback_ingest import run

        golden = tmp_path / "golden.jsonl"
        golden.write_text(
            json.dumps({"id": "L001", "question": "q"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        before = golden.read_text(encoding="utf-8")

        inp = tmp_path / "prod.jsonl"
        inp.write_text(
            json.dumps({"question": "bad", "status": "error"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        out_dir = tmp_path / "candidates"

        rc = run(inp, out_dir=out_dir, golden_path=golden, timestamp="TEST")
        assert rc == 0
        assert golden.read_text(encoding="utf-8") == before, "golden 不得被改动"

        out_file = out_dir / "TEST.candidates.jsonl"
        assert out_file.exists()
        rows = [json.loads(line) for line in out_file.read_text("utf-8").splitlines()]
        assert len(rows) == 1
        assert rows[0]["_needs_review"] is True

    def test_run_reports_no_failure(self, tmp_path: Path, capsys):
        from evals.feedback_ingest import run

        inp = tmp_path / "prod.jsonl"
        inp.write_text(
            json.dumps({"question": "ok", "route_type": "kb-query", "message": "x"})
            + "\n",
            encoding="utf-8",
        )
        rc = run(inp, out_dir=tmp_path / "c", golden_path=tmp_path / "g.jsonl")
        assert rc == 0
        assert "未发现失败样本" in capsys.readouterr().out

    def test_run_missing_input(self, tmp_path: Path):
        from evals.feedback_ingest import run

        assert run(tmp_path / "nope.jsonl", out_dir=tmp_path / "c") == 2

    def test_next_candidate_index_avoids_id_reuse(self, tmp_path: Path):
        from evals.feedback_ingest import next_candidate_index

        golden = tmp_path / "g.jsonl"
        golden.write_text(
            "\n".join(
                [
                    json.dumps({"id": "L001"}),
                    json.dumps({"id": "C007"}),
                    json.dumps({"id": "L002"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        assert next_candidate_index(golden) == 8


# ---------------------------------------------------------------------------
# 轨迹持久化（JSONL sink）：让 F1 回放 / F5 回归真正可用
# ---------------------------------------------------------------------------


class TestJsonlTrajectorySink:
    def _sink(self, tmp_path: Path):
        from agentix.application.services.trajectory import JsonlTrajectorySink

        return JsonlTrajectorySink(tmp_path / "sub" / "traj.jsonl")

    def test_append_and_read_back(self, tmp_path: Path):
        """写入的步骤必须能原样读回（跨进程持久化的基础）。"""
        sink = self._sink(tmp_path)
        rec = TrajectoryRecorder(sink=sink)
        rec.record(kind="node", name="guardrails", trace_id="t1")
        rec.record(kind="llm", name="router", trace_id="t1")

        steps = sink.list("t1")
        assert [s.name for s in steps] == ["guardrails", "router"]
        assert steps[0].kind == "node"

    def test_traces_are_isolated_on_disk(self, tmp_path: Path):
        sink = self._sink(tmp_path)
        rec = TrajectoryRecorder(sink=sink)
        rec.record(kind="node", name="a", trace_id="t1")
        rec.record(kind="node", name="b", trace_id="t2")

        assert [s.name for s in sink.list("t1")] == ["a"]
        assert [s.name for s in sink.list("t2")] == ["b"]

    def test_data_survives_a_new_sink_instance(self, tmp_path: Path):
        """核心：新的 sink 实例（模拟进程重启）仍能读到旧数据。"""
        path = tmp_path / "traj.jsonl"
        first = TrajectoryRecorder(
            sink=__import__(
                "agentix.application.services.trajectory", fromlist=["x"]
            ).JsonlTrajectorySink(path)
        )
        first.record(kind="tool", name="cypher_query", trace_id="persist")

        # 全新的 recorder + sink，等价于进程重启
        from agentix.application.services.trajectory import JsonlTrajectorySink

        second = TrajectoryRecorder(sink=JsonlTrajectorySink(path))
        assert [s.name for s in second.get("persist")] == ["cypher_query"]

    def test_corrupted_line_is_skipped(self, tmp_path: Path):
        """写一半的损坏行不得让整个文件不可读。"""
        path = tmp_path / "traj.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"step_index": 0, "kind": "node", "name": "ok", "trace_id": "t"}\n'
            '{"broken...\n'
            '{"step_index": 1, "kind": "llm", "name": "also_ok", "trace_id": "t"}\n',
            encoding="utf-8",
        )
        from agentix.application.services.trajectory import JsonlTrajectorySink

        steps = JsonlTrajectorySink(path).list("t")
        assert [s.name for s in steps] == ["ok", "also_ok"]

    def test_missing_file_reads_as_empty(self, tmp_path: Path):
        from agentix.application.services.trajectory import JsonlTrajectorySink

        assert JsonlTrajectorySink(tmp_path / "nope.jsonl").list("t") == []

    def test_all_traces_groups_by_trace_id(self, tmp_path: Path):
        sink = self._sink(tmp_path)
        rec = TrajectoryRecorder(sink=sink)
        rec.record(kind="node", name="a", trace_id="t1")
        rec.record(kind="node", name="b", trace_id="t2")
        grouped = sink.all_traces()
        assert sorted(grouped) == ["t1", "t2"]

    def test_rotation_keeps_one_backup(self, tmp_path: Path):
        from agentix.application.services.trajectory import JsonlTrajectorySink

        path = tmp_path / "traj.jsonl"
        sink = JsonlTrajectorySink(path, max_bytes=512)
        rec = TrajectoryRecorder(sink=sink)
        for i in range(60):
            rec.record(kind="node", name=f"step{i}", trace_id="t1")
        # 触发过轮转后，仍应可读且产生了一份 .1 备份
        assert path.exists()
        assert len(sink.list("t1")) <= 200

    def test_clear_removes_file(self, tmp_path: Path):
        sink = self._sink(tmp_path)
        rec = TrajectoryRecorder(sink=sink)
        rec.record(kind="node", name="a", trace_id="t1")
        sink.clear()
        assert sink.list("t1") == []

    def test_stats(self, tmp_path: Path):
        sink = self._sink(tmp_path)
        rec = TrajectoryRecorder(sink=sink)
        rec.record(kind="node", name="a", trace_id="t1")
        stats = sink.stats()
        assert stats["bytes"] > 0
        assert stats["exists"] is True


class TestExportTrajectoryPackages:
    def test_export_creates_consumable_packages(self, tmp_path: Path):
        """导出的轨迹包必须能被轨迹前缀回归 CLI 直接消费。"""
        from agentix.application.services.trajectory import (
            JsonlTrajectorySink,
            export_trajectory_packages,
        )

        sink = JsonlTrajectorySink(tmp_path / "traj.jsonl")
        rec = TrajectoryRecorder(sink=sink)
        for name in ("guardrails", "router", "cypher_query"):
            rec.record(kind="tool", name=name, trace_id="q1")

        out = tmp_path / "baselines"
        count = export_trajectory_packages(sink, out)
        assert count == 1
        package = json.loads((out / "q1.json").read_text(encoding="utf-8"))
        assert package["trace_id"] == "q1"
        assert package["step_count"] == 3
        assert [s["name"] for s in package["steps"]] == [
            "guardrails",
            "router",
            "cypher_query",
        ]

    def test_export_from_recorder(self, tmp_path: Path):
        from agentix.application.services.trajectory import export_trajectory_packages

        rec = TrajectoryRecorder()
        rec.record(kind="node", name="a", trace_id="r1")
        count = export_trajectory_packages(rec, tmp_path / "out")
        assert count == 1
        assert (tmp_path / "out" / "r1.json").exists()

    def test_export_marks_first_error(self, tmp_path: Path):
        from agentix.application.services.trajectory import export_trajectory_packages

        rec = TrajectoryRecorder()
        rec.record(kind="node", name="a", status="ok", trace_id="e1")
        rec.record(kind="tool", name="bad", status="error", error="boom", trace_id="e1")
        export_trajectory_packages(rec, tmp_path / "out")
        pkg = json.loads((tmp_path / "out" / "e1.json").read_text(encoding="utf-8"))
        assert pkg["first_error_step_index"] == 1

    def test_export_empty_sink_returns_zero(self, tmp_path: Path):
        from agentix.application.services.trajectory import (
            JsonlTrajectorySink,
            export_trajectory_packages,
        )

        assert export_trajectory_packages(JsonlTrajectorySink(tmp_path / "x.jsonl"), tmp_path / "o") == 0

    def test_export_rejects_unsupported_sink(self, tmp_path: Path):
        from agentix.application.services.trajectory import export_trajectory_packages

        with pytest.raises(TypeError):
            export_trajectory_packages(object(), tmp_path / "o")  # type: ignore[arg-type]


class TestBuildTrajectorySink:
    def test_default_is_memory(self, monkeypatch):
        from agentix.application.services.trajectory import (
            InMemoryTrajectorySink,
            build_trajectory_sink,
        )
        from agentix.config import settings

        monkeypatch.setattr(settings, "TRAJECTORY_SINK", "memory", raising=False)
        assert isinstance(build_trajectory_sink(), InMemoryTrajectorySink)

    def test_jsonl_selected_by_setting(self, monkeypatch, tmp_path: Path):
        from agentix.application.services.trajectory import (
            JsonlTrajectorySink,
            build_trajectory_sink,
        )
        from agentix.config import settings

        monkeypatch.setattr(settings, "TRAJECTORY_SINK", "jsonl", raising=False)
        monkeypatch.setattr(
            settings, "TRAJECTORY_PATH", str(tmp_path / "t.jsonl"), raising=False
        )
        assert isinstance(build_trajectory_sink(), JsonlTrajectorySink)

    def test_jsonl_without_path_falls_back_to_memory(self, monkeypatch):
        from agentix.application.services.trajectory import (
            InMemoryTrajectorySink,
            build_trajectory_sink,
        )
        from agentix.config import settings

        monkeypatch.setattr(settings, "TRAJECTORY_SINK", "jsonl", raising=False)
        monkeypatch.setattr(settings, "TRAJECTORY_PATH", "", raising=False)
        assert isinstance(build_trajectory_sink(), InMemoryTrajectorySink)

    def test_unknown_kind_falls_back_to_memory(self, monkeypatch):
        from agentix.application.services.trajectory import (
            InMemoryTrajectorySink,
            build_trajectory_sink,
        )
        from agentix.config import settings

        monkeypatch.setattr(settings, "TRAJECTORY_SINK", "redis?!", raising=False)
        assert isinstance(build_trajectory_sink(), InMemoryTrajectorySink)


class TestRecorderTraceIds:
    def test_trace_ids_with_memory_sink(self):
        rec = TrajectoryRecorder()
        rec.record(kind="node", name="a", trace_id="x")
        rec.record(kind="node", name="b", trace_id="y")
        assert sorted(rec.trace_ids()) == ["x", "y"]

    def test_trace_ids_with_jsonl_sink(self, tmp_path: Path):
        from agentix.application.services.trajectory import JsonlTrajectorySink

        rec = TrajectoryRecorder(sink=JsonlTrajectorySink(tmp_path / "t.jsonl"))
        rec.record(kind="node", name="a", trace_id="x")
        assert rec.trace_ids() == ["x"]


# ---------------------------------------------------------------------------
# F5：轨迹前缀回归（双层回归的第二层）
# ---------------------------------------------------------------------------


def _steps(*specs, trace_id: str = "t") -> List[TrajectoryStep]:
    """按 (kind, name, status) 快速造步骤序列。"""
    out: List[TrajectoryStep] = []
    for idx, spec in enumerate(specs):
        kind, name = spec[0], spec[1]
        status = spec[2] if len(spec) > 2 else "ok"
        out.append(
            TrajectoryStep(
                step_index=idx, kind=kind, name=name, status=status, trace_id=trace_id
            )
        )
    return out


class TestTrajectoryPrefixSignature:
    def test_signature_is_sorted_and_stringified(self):
        from agentix.application.services.trajectory import (
            trajectory_prefix_signature,
        )

        # 刻意让入参顺序与 step_index 顺序相反
        later = TrajectoryStep(step_index=1, kind="llm", name="a", trace_id="t")
        earlier = TrajectoryStep(step_index=0, kind="node", name="b", trace_id="t")
        sig = trajectory_prefix_signature([later, earlier])
        assert sig == [["node", "b", "ok"], ["llm", "a", "ok"]]

    def test_signature_length_cap(self):
        from agentix.application.services.trajectory import (
            trajectory_prefix_signature,
        )

        steps = _steps(("node", "a"), ("node", "b"), ("node", "c"))
        assert len(trajectory_prefix_signature(steps, length=2)) == 2

    def test_signature_custom_fields(self):
        from agentix.application.services.trajectory import (
            trajectory_prefix_signature,
        )

        steps = _steps(("node", "a"), ("llm", "b", "error"))
        sig = trajectory_prefix_signature(steps, fields=("name", "status"))
        assert sig == [["a", "ok"], ["b", "error"]]


class TestCompareTrajectoryPrefix:
    def test_identical_trajectories_match(self):
        from agentix.application.services.trajectory import compare_trajectory_prefix

        specs = [("node", "guardrails"), ("llm", "router"), ("tool", "cypher")]
        result = compare_trajectory_prefix(_steps(*specs), _steps(*specs))
        assert result["match"] is True
        assert result["first_divergence_index"] is None
        assert result["reason"] == "ok"
        assert result["compared_length"] == 3

    def test_longer_candidate_still_matches(self):
        """前缀语义：候选多走几步不算回归。"""
        from agentix.application.services.trajectory import compare_trajectory_prefix

        base = _steps(("node", "guardrails"), ("llm", "router"))
        cand = _steps(("node", "guardrails"), ("llm", "router"), ("tool", "extra"))
        result = compare_trajectory_prefix(base, cand)
        assert result["match"] is True
        assert result["candidate_length"] == 3

    def test_shorter_candidate_diverges(self):
        from agentix.application.services.trajectory import compare_trajectory_prefix

        base = _steps(("node", "guardrails"), ("llm", "router"), ("tool", "cypher"))
        cand = _steps(("node", "guardrails"), ("llm", "router"))
        result = compare_trajectory_prefix(base, cand)
        assert result["match"] is False
        assert result["reason"] == "candidate_shorter"
        assert result["first_divergence_index"] == 2
        assert result["candidate_step"] is None
        assert result["baseline_step"] == ["tool", "cypher", "ok"]

    def test_step_mismatch_reports_first_divergence(self):
        from agentix.application.services.trajectory import compare_trajectory_prefix

        base = _steps(("node", "guardrails"), ("llm", "router"), ("tool", "cypher"))
        cand = _steps(("node", "guardrails"), ("llm", "OTHER"), ("tool", "cypher"))
        result = compare_trajectory_prefix(base, cand)
        assert result["match"] is False
        assert result["reason"] == "step_mismatch"
        assert result["first_divergence_index"] == 1
        assert result["baseline_step"] == ["llm", "router", "ok"]
        assert result["candidate_step"] == ["llm", "OTHER", "ok"]

    def test_length_limit_ignores_downstream_drift(self):
        """只锁定上游 N 步时，下游漂移不应判失败。"""
        from agentix.application.services.trajectory import compare_trajectory_prefix

        base = _steps(("node", "guardrails"), ("llm", "router"), ("tool", "cypher"))
        cand = _steps(("node", "guardrails"), ("llm", "router"), ("tool", "CHANGED"))
        assert compare_trajectory_prefix(base, cand)["match"] is False
        assert compare_trajectory_prefix(base, cand, length=2)["match"] is True

    def test_empty_baseline_is_flagged_not_silently_ok(self):
        """空真不得冒充回归保障 —— 必须显式标注 empty_baseline。"""
        from agentix.application.services.trajectory import compare_trajectory_prefix

        result = compare_trajectory_prefix([], _steps(("node", "a")))
        assert result["match"] is True
        assert result["reason"] == "empty_baseline"
        assert result["compared_length"] == 0

    def test_status_change_is_detected(self):
        """路径相同但某步由 ok 变 error —— 属回归。"""
        from agentix.application.services.trajectory import compare_trajectory_prefix

        base = _steps(("node", "a"), ("tool", "milvus"))
        cand = _steps(("node", "a"), ("tool", "milvus", "error"))
        result = compare_trajectory_prefix(base, cand)
        assert result["match"] is False
        assert result["first_divergence_index"] == 1


class TestCompareTrajectoryPackages:
    def test_replay_package_roundtrip(self):
        from agentix.application.services.trajectory import (
            TrajectoryRecorder,
            compare_trajectory_packages,
        )

        rec = TrajectoryRecorder()
        rec.record(kind="node", name="guardrails", trace_id="t1")
        rec.record(kind="llm", name="router", trace_id="t1")
        base_pkg = rec.replay("t1")

        rec2 = TrajectoryRecorder()
        rec2.record(kind="node", name="guardrails", trace_id="t2")
        rec2.record(kind="llm", name="router", trace_id="t2")
        cand_pkg = rec2.replay("t2")

        result = compare_trajectory_packages(base_pkg, cand_pkg)
        assert result["match"] is True

    def test_package_accepts_step_objects(self):
        from agentix.application.services.trajectory import (
            compare_trajectory_packages,
        )

        base = {"trace_id": "a", "steps": _steps(("node", "x"))}
        cand = {"trace_id": "b", "steps": [s.to_dict() for s in _steps(("node", "x"))]}
        assert compare_trajectory_packages(base, cand)["match"] is True

    def test_package_missing_steps_key_treated_as_empty(self):
        from agentix.application.services.trajectory import (
            compare_trajectory_packages,
        )

        result = compare_trajectory_packages({}, {"steps": []})
        assert result["reason"] == "empty_baseline"


class TestTrajectoryRegressionCLI:
    def _write(self, path: Path, trace_id: str, specs) -> Path:
        payload = {
            "trace_id": trace_id,
            "step_count": len(specs),
            "steps": [s.to_dict() for s in _steps(*specs, trace_id=trace_id)],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_run_regression_counts_and_missing(self, tmp_path: Path):
        from evals.trajectory_regression import run_regression

        base_dir = tmp_path / "base"
        cand_dir = tmp_path / "cand"
        base_dir.mkdir()
        cand_dir.mkdir()
        self._write(base_dir / "a.json", "a", [("node", "g"), ("llm", "r")])
        self._write(base_dir / "b.json", "b", [("node", "g"), ("llm", "r")])
        # a 匹配；b 候选缺失
        self._write(cand_dir / "a.json", "a", [("node", "g"), ("llm", "r"), ("tool", "x")])
        # 候选独有 c
        self._write(cand_dir / "c.json", "c", [("node", "g")])

        from evals.trajectory_regression import load_packages

        report = run_regression(load_packages(base_dir), load_packages(cand_dir))
        assert report["total_baselines"] == 2
        assert report["matched"] == 1
        assert report["missing_candidate"] == 1
        assert report["ok"] is False
        assert report["extra_candidates"] == ["c"]

    def test_cli_exit_codes(self, tmp_path: Path, capsys):
        from evals.trajectory_regression import main

        base = self._write(tmp_path / "base.json", "t", [("node", "g"), ("llm", "r")])
        same = self._write(tmp_path / "same.json", "t", [("node", "g"), ("llm", "r")])
        diff = self._write(tmp_path / "diff.json", "t", [("node", "g"), ("llm", "OTHER")])

        assert main(["-b", str(base), "-c", str(same)]) == 0
        assert "轨迹前缀一致" in capsys.readouterr().out

        assert main(["-b", str(base), "-c", str(diff)]) == 1
        out = capsys.readouterr().out
        assert "轨迹前缀分歧" in out
        assert "llm" in out and "OTHER" in out

    def test_cli_length_flag_restores_match(self, tmp_path: Path, capsys):
        from evals.trajectory_regression import main

        base = self._write(tmp_path / "b.json", "t", [("node", "g"), ("llm", "r"), ("tool", "c")])
        diff = self._write(tmp_path / "d.json", "t", [("node", "g"), ("llm", "r"), ("tool", "CHANGED")])

        assert main(["-b", str(base), "-c", str(diff)]) == 1
        assert main(["-b", str(base), "-c", str(diff), "--length", "2"]) == 0

    def test_cli_writes_report_and_bad_path(self, tmp_path: Path, capsys):
        from evals.trajectory_regression import main

        base = self._write(tmp_path / "b.json", "t", [("node", "g")])
        cand = self._write(tmp_path / "c.json", "t", [("node", "g")])
        out = tmp_path / "reports" / "r.json"

        assert main(["-b", str(base), "-c", str(cand), "--out", str(out)]) == 0
        assert out.exists()
        assert json.loads(out.read_text(encoding="utf-8"))["matched"] == 1

        assert main(["-b", str(tmp_path / "nope"), "-c", str(cand)]) == 2
        assert "路径不存在" in capsys.readouterr().err

    def test_load_packages_rejects_non_package(self, tmp_path: Path):
        from evals.trajectory_regression import load_packages

        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"trace_id": "t"}), encoding="utf-8")
        with pytest.raises(ValueError, match="steps"):
            load_packages(bad)

