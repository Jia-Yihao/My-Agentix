"""性能基准与门禁的结构守卫（p93）。

覆盖：

1. queries.jsonl 九类负载完整、class 唯一、http 路径合法；
2. ``run_benchmark``（跳过 HTTP 族，进程内两族）结果结构合法：
   p50 ≤ p95 ≤ max、count == iterations、meta 口径齐全；
3. 统计口径：最近秩百分位的确定性数值；
4. perf_gate：回归可检出、无回归通过、新类不误杀、基线缺失跳过、
   ``--record`` 录制往返一致。

HTTP 族的端到端执行与耗时软断言在 tests/test_perf_smoke.py（CI perf-smoke）。
"""

from __future__ import annotations

import json

import pytest

from evals.performance.perf_gate import compare, record_baseline, run_gate
from evals.performance.runner import load_queries, run_benchmark, summarize, write_results


class TestQueries:
    def test_nine_classes_present(self):
        queries = load_queries()
        assert len(queries) == 9
        classes = [q["class"] for q in queries]
        assert len(set(classes)) == 9, "class 必须唯一（门禁按 class 对齐）"

    def test_kinds_cover_three_families(self):
        queries = load_queries()
        kinds = {q["kind"] for q in queries}
        assert kinds == {"http", "stream", "cpu"}

    def test_http_paths_are_valid(self):
        for q in load_queries():
            if q["kind"] == "http":
                assert q["path"].startswith("/")
                assert q.get("method", "GET") in {"GET", "POST"}

    def test_invalid_jsonl_rejected(self, tmp_path):
        bad = tmp_path / "q.jsonl"
        bad.write_text("{not json}\n", encoding="utf-8")
        with pytest.raises(ValueError):
            load_queries(bad)


class TestStatistics:
    def test_percentile_nearest_rank(self):
        """最近邻秩的确定性数值（唯一实现：``evals/perf_stats``）。

        2026-10-27（T2）本 runner 的秩下标由 ``sorted[round(p/100*n)-1]`` 换成
        全仓统一的 ``sorted[round(q*(n-1))]``：n=100 时 p50 由第 50 小变成第 51 小。
        这是**口径切换的账面代价**，历史 ``baseline.json`` 因此不可直接比，
        判门禁前应按新口径重录（``perf_gate.py --record``）。
        """
        samples = [float(i) for i in range(1, 101)]  # 1..100
        stats = summarize(samples)
        assert stats["p50"] == 51.0
        assert stats["p90"] == 90.0
        assert stats["p95"] == 95.0
        assert stats["p99"] == 99.0
        assert stats["max"] == 100.0
        assert stats["count"] == 100.0
        assert stats["qps"] == round(1000.0 / stats["mean"], 2)
        assert stats["p50"] <= stats["p90"] <= stats["p95"] <= stats["p99"] <= stats["max"]

    def test_empty_samples_safe(self):
        stats = summarize([])
        assert stats == {
            "p50": 0.0,
            "p90": 0.0,
            "p95": 0.0,
            "p99": 0.0,
            "max": 0.0,
            "mean": 0.0,
            "qps": 0.0,
            "count": 0.0,
        }


class TestRunBenchmark:
    def test_stream_and_cpu_structure(self):
        results = run_benchmark(1, http=False)
        assert results["meta"]["iterations"] == 1
        assert results["meta"]["kinds"] == ["cpu", "stream"]
        assert results["meta"]["protocol"]

        classes = {row["class"] for row in results["queries"]}
        assert classes == {"stream_answer", "stream_progress", "emitter_cpu"}
        for row in results["queries"]:
            assert row["count"] == 1.0
            assert 0 <= row["p50"] <= row["p95"] <= row["max"]
        assert results["overall"]["count"] == 3.0

    def test_iterations_must_be_positive(self):
        with pytest.raises(ValueError):
            run_benchmark(0, http=False)

    def test_write_results_roundtrip(self, tmp_path):
        results = run_benchmark(1, http=False)
        out = write_results(results, tmp_path / "r.json")
        loaded = json.loads(out.read_text(encoding="utf-8"))
        assert loaded["queries"] == results["queries"]


class TestGate:
    def _results(self, p50: float) -> dict:
        return {
            "meta": {"iterations": 3, "protocol": "test"},
            "queries": [{"class": "x", "kind": "cpu", "p50": p50, "p95": p50, "max": p50, "count": 3}],
            "overall": {"p50": p50, "p95": p50, "max": p50, "count": 3},
        }

    def test_regression_detected(self):
        baseline = self._results(10.0)
        problems = compare(self._results(100.0), baseline, ratio=3.0)
        assert len(problems) == 2  # 单类 + overall
        assert "x:" in problems[0]

    def test_no_regression_passes(self):
        baseline = self._results(10.0)
        assert compare(self._results(15.0), baseline, ratio=3.0) == []

    def test_new_class_not_fatal(self):
        baseline = self._results(10.0)
        now = self._results(10.0)
        now["queries"].append(
            {"class": "brand_new", "kind": "http", "p50": 1.0, "p95": 1.0, "max": 1.0, "count": 3}
        )
        assert compare(now, baseline, ratio=3.0) == []

    def test_missing_class_skipped_not_fatal(self):
        baseline = self._results(10.0)
        baseline["queries"].append(
            {"class": "gone", "kind": "http", "p50": 1.0, "p95": 1.0, "max": 1.0, "count": 3}
        )
        assert compare(self._results(10.0), baseline, ratio=3.0) == []

    def test_missing_baseline_file_skips(self, tmp_path):
        rc = run_gate(
            results=self._results(10.0),
            baseline_path=tmp_path / "nope.json",
        )
        assert rc == 0

    def test_record_then_gate_pass(self, tmp_path):
        results = self._results(10.0)
        results_file = tmp_path / "res.json"
        results_file.write_text(json.dumps(results), encoding="utf-8")
        baseline_file = tmp_path / "base.json"
        record_baseline(results_file, baseline_file)
        assert baseline_file.exists()
        rc = run_gate(results=results, baseline_path=baseline_file)
        assert rc == 0

    def test_record_then_gate_regression(self, tmp_path):
        results_file = tmp_path / "res.json"
        results_file.write_text(json.dumps(self._results(10.0)), encoding="utf-8")
        baseline_file = tmp_path / "base.json"
        record_baseline(results_file, baseline_file)
        rc = run_gate(results=self._results(999.0), baseline_path=baseline_file)
        assert rc == 1

    def test_committed_baseline_is_loadable(self):
        """仓库提交的 baseline.json 必须与门禁口径兼容（meta+queries+overall）。"""
        from evals.performance.perf_gate import DEFAULT_BASELINE, load_json

        baseline = load_json(DEFAULT_BASELINE)
        assert baseline["meta"]["iterations"] >= 1
        assert len(baseline["queries"]) == 9
        assert baseline["overall"]["p50"] > 0
