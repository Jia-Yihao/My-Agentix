"""P104：promptfoo 集成契约守卫（P1-5）。

钉死三件事：
① promptfooconfig.yaml 可解析且引用的 provider/assert/tests 路径真实存在
② 转换脚本产物 = golden 子集（纯机械映射、可复现、期望零加工）
③ 断言文件 import 的是 run_eval **真函数**（打分口径单源，防抄写第二份）
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

CONFIG_PATH = PROJECT_ROOT / "promptfooconfig.yaml"
CONVERTER = "evals/golden_to_promptfoo.py"
ASSERTS = "evals/promptfoo_asserts.py"
PROVIDER = "evals/promptfoo_provider.py"


# ---------- ① 配置可解析 + 引用面 ----------

class TestConfigContract:
    def test_config_parses(self):
        yaml = pytest.importorskip("yaml")
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert data.get("providers"), "缺 providers"
        assert data.get("tests"), "缺 tests"

    def test_referenced_files_exist(self):
        for path in (PROVIDER, ASSERTS, "evals/promptfoo_tests.jsonl"):
            assert (PROJECT_ROOT / path).exists(), f"配置引用的文件缺失：{path}"

    def test_provider_and_asserts_importable(self):
        """provider/断言模块可被真实 import（promptfoo worker 加载的前提）。"""
        from evals import promptfoo_asserts, promptfoo_provider  # noqa: F401

    def test_asserts_use_run_eval_scores(self):
        """口径单源（源级棘轮）：断言文件必须 import run_eval 的打分函数，
        且文件内**没有**这些函数的实现副本（防抄写第二份口径后漂移）。"""
        src = (PROJECT_ROOT / ASSERTS).read_text(encoding="utf-8")
        assert "from evals.run_eval import" in src
        for fn in ("score_route", "score_citations", "score_reject", "score_must_contain"):
            assert fn in src
        # 不许出现 def score_route( 之类的本地实现副本
        copied = re.findall(r"def (score_\w+)\(", src)
        assert not copied, f"断言文件内出现打分函数实现副本（口径必须单源）：{copied}"


# ---------- ② 转换产物 = golden 子集 ----------

class TestConverterContract:
    @pytest.fixture(scope="class")
    def converted(self, tmp_path_factory):
        """真实跑一次转换（写入 class 级 tmp），供断言复用。"""
        from evals.golden_to_promptfoo import main as convert_main

        out = tmp_path_factory.mktemp("pf") / "promptfoo_tests.jsonl"
        rc = convert_main(["--out", str(out), "--limit", "25"])
        assert rc == 0
        rows = [
            json.loads(line)
            for line in out.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return out, rows

    def test_row_count_matches_golden_subset(self, converted):
        """产物条数 == golden 子集条数（25 条试点，确定性断言）。"""
        _, rows = converted
        assert len(rows) == 25

    def test_reproducible(self, converted, tmp_path):
        """同一输入两次转换逐字节一致（取前 N 而非随机抽样的可复现性）。"""
        from evals.golden_to_promptfoo import main as convert_main

        out, rows = converted
        again = tmp_path / "again.jsonl"
        assert convert_main(["--out", str(again), "--limit", "25"]) == 0
        assert again.read_text(encoding="utf-8") == out.read_text(encoding="utf-8")

    def test_expectations_come_from_golden_only(self, converted):
        """vars 里的期望值必须与 golden 原行一致（机械映射，零语义加工）。"""
        _, rows = converted
        golden_rows = [
            json.loads(line)
            for line in (PROJECT_ROOT / "evals/legal_golden.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        golden_by_q = {g["question"]: g for g in golden_rows}
        for row in rows[:5]:
            q = row["vars"]["question"]
            g = golden_by_q[q]
            assert row["vars"]["expected_route"] == str(g.get("route_type") or "")
            assert row["vars"]["expect_reject"] == ("true" if g.get("expect_reject") else "false")
            assert json.loads(row["vars"]["expected_sources"]) == [
                str(s) for s in (g.get("expected_sources") or [])
            ]

    def test_asserts_reference_get_assert_file(self, converted):
        """每条用例的断言都指向 file://evals/promptfoo_asserts.py（get_assert）。"""
        _, rows = converted
        for row in rows:
            assert any(
                a.get("value") == f"file://{ASSERTS}" for a in row.get("assert", [])
            )


# ---------- ③ get_assert 行为 ----------

class TestGetAssert:
    def test_perfect_output_passes(self):
        from evals.promptfoo_asserts import get_assert

        output = json.dumps(
            {"route": "kb-query", "sources": ["民法典第577条"], "message": "答案", "status": "ok"},
            ensure_ascii=False,
        )
        result = get_assert(output, {"vars": {
            "expected_route": "kb-query",
            "expect_reject": "false",
            "expected_sources": json.dumps(["民法典"]),
            "must_contain": "",
        }})
        assert result["pass"] is True
        assert result["score"] == 1.0

    def test_bad_route_fails(self):
        from evals.promptfoo_asserts import get_assert

        output = json.dumps({"route": "general-query", "sources": [], "message": "x", "status": "ok"})
        result = get_assert(output, {"vars": {"expected_route": "kb-query"}})
        assert result["pass"] is False
        assert result["score"] < 1.0

    def test_provider_error_fails(self):
        from evals.promptfoo_asserts import get_assert

        output = json.dumps({"error": "RuntimeError: boom"})
        result = get_assert(output, {"vars": {}})
        assert result["pass"] is False

    def test_invalid_json_fails(self):
        from evals.promptfoo_asserts import get_assert

        result = get_assert("not json", {"vars": {}})
        assert result["pass"] is False
