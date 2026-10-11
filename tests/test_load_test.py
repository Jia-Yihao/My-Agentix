"""``scripts/load_test.py`` 守卫（性能门禁的执行体）。

本文件存在的理由：``perf-baseline.yml`` 每天调用该脚本并把 ``p95_seconds``
与基线比对，但脚本**曾经只被引用、从未被实现**——workflow 一跑就崩，
"性能门禁"从来没运行过。这里把它的契约钉住，防止再次悄悄消失。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.load_test import main, percentile, run_load, summarize  # noqa: E402


# ---------------------------------------------------------------------------
# 统计：分位数与空样本语义
# ---------------------------------------------------------------------------


class TestPercentile:
    def test_empty_returns_none_not_zero(self):
        """★ 无数据必须是 None，不是 0.0——"没跑成" ≠ "全都很快"。"""
        assert percentile([], 95) is None
        assert summarize([])["p95"] is None
        assert summarize([])["count"] == 0

    def test_single_sample(self):
        assert percentile([0.5], 95) == 0.5

    def test_linear_interpolation(self):
        values = [1.0, 2.0, 3.0, 4.0]
        assert percentile(values, 0) == 1.0
        assert percentile(values, 100) == 4.0
        assert percentile(values, 50) == pytest.approx(2.5)

    def test_p95_and_p99_are_distinguishable(self):
        """最近秩会让小样本的 p95 == p99，丢掉尾部信息；线性插值必须分得开。"""
        values = [float(i) for i in range(1, 12)]  # 1..11
        assert percentile(values, 95) != percentile(values, 99)

    def test_summarize_orders(self):
        summary = summarize([1.0, 2.0, 3.0, 4.0, 5.0])
        assert summary["min"] <= summary["p50"] <= summary["p90"] <= summary["p95"] <= summary["max"]
        assert summary["count"] == 5


# ---------------------------------------------------------------------------
# 端到端：对着真实本地 HTTP 服务压一遍
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - 标准库约定的方法名
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, *args):  # 静音：避免污染 pytest 输出
        return


@pytest.fixture(scope="module")
def local_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/liveness"
    server.shutdown()
    server.server_close()


class TestRunLoadEndToEnd:
    def test_happy_path_report_shape(self, local_server):
        report, code = run_load(
            local_server, requests_total=20, workers=4, timeout=5.0
        )
        assert code == 0
        assert report["success"] == 20
        assert report["errors"] == 0
        # workflow 依赖的顶层键必须存在且是数字
        assert isinstance(report["p95_seconds"], float)
        assert report["p95_seconds"] > 0
        assert report["status_codes"] == {"200": 20}
        assert report["requests"] == 20 and report["workers"] == 4
        assert set(report["latency_seconds"]) >= {"min", "mean", "p50", "p90", "p95", "p99", "max"}

    def test_connection_refused_yields_null_p95_and_nonzero_exit(self):
        """★ 打不通的服务：p95 必须是 null（不是 0.0），退出码必须非零。"""
        report, code = run_load(
            "http://127.0.0.1:9/never", requests_total=3, workers=1, timeout=0.5
        )
        assert code == 2
        assert report["success"] == 0
        assert report["p95_seconds"] is None
        assert report["latency_seconds"]["p95"] is None
        assert report["errors"] == 3
        assert report["throughput_rps"] is None

    def test_workers_never_exceed_requests(self, local_server):
        report, code = run_load(local_server, requests_total=2, workers=10, timeout=5.0)
        assert code == 0
        assert report["workers"] == 2

    def test_bad_scheme_raises(self):
        with pytest.raises(ValueError):
            run_load("ftp://example.com/x", requests_total=1, workers=1, timeout=1.0)


# ---------------------------------------------------------------------------
# CLI：stdout 必须是唯一且合法的 JSON（workflow 会 tee 成 baseline.json）
# ---------------------------------------------------------------------------


class TestCliContract:
    def test_stdout_is_pure_json(self, local_server, capsys):
        code = main(["--url", local_server, "--requests", "5", "--workers", "2"])
        captured = capsys.readouterr()
        assert code == 0
        payload = json.loads(captured.out)  # 严格解析：多一行日志都会失败
        assert payload["success"] == 5

    def test_zero_success_still_emits_valid_json(self, capsys):
        code = main(["--url", "http://127.0.0.1:9/never", "--requests", "2", "--timeout", "0.3"])
        captured = capsys.readouterr()
        assert code == 2
        payload = json.loads(captured.out)
        assert payload["p95_seconds"] is None
        assert "::error" in captured.err

    def test_bad_header_format_is_rejected(self, capsys):
        code = main(["--url", "http://127.0.0.1:9/x", "--header", "no-colon"])
        captured = capsys.readouterr()
        assert code == 2
        assert "K:V" in captured.err


def test_script_is_runnable_as_a_module_entrypoint(local_server):
    """workflow 用 `python scripts/load_test.py` 调用——必须真能当脚本跑。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "load_test.py"),
         "--url", local_server, "--requests", "3", "--workers", "1"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["success"] == 3
