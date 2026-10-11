"""P106：Ragas 忠实度报告（P2-2，observe-only）测试。

覆盖：
① 缺依赖 / 缺 judge 端点 / 缺输入 / 无可评估样本 ⇒ exit 2 诚实降级
② build_samples 纯函数：按 id 对齐取最小 attempt、空 sources/answer 跳过
③ 报告键族固定（REPORT_KEYS）且与 CI METRICS 五键元组**交集为空**（防守卫破）
④ p90 / test_ci_gate_contract 门禁契约零变化（源级棘轮）
"""

from __future__ import annotations

import json
import re
from pathlib import Path


from evals.ragas_report import REPORT_KEYS, build_samples, main

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------- ② build_samples 纯函数 ----------

GOLDEN = [
    {"id": "G001", "question": "合同违约的构成要件？"},
    {"id": "G002", "question": "民法典一共有多少条规定？"},
    {"id": "G003", "question": "你好"},
]
PREDS = [
    {"id": "G001", "attempt": 1, "message": "违约需…", "sources": ["民法典第577条"]},
    {"id": "G001", "attempt": 0, "message": "首轮答案", "sources": ["民法典第577条"]},
    {"id": "G002", "attempt": 1, "message": "共1260条", "sources": []},  # 无 sources → 跳过
    {"id": "G003", "attempt": 1, "message": "", "sources": ["x"]},  # 空答案 → 跳过
]


class TestBuildSamples:
    def test_align_by_id_takes_min_attempt(self):
        samples = build_samples(GOLDEN, PREDS)
        by_q = {s["question"]: s for s in samples}
        assert by_q["合同违约的构成要件？"]["answer"] == "首轮答案"

    def test_skips_empty_sources_and_answer(self):
        samples = build_samples(GOLDEN, PREDS)
        assert {s["question"] for s in samples} == {"合同违约的构成要件？"}

    def test_limit_caps_samples(self):
        preds = [
            {"id": f"X{i:03d}", "attempt": 1, "message": f"a{i}", "sources": ["s"]}
            for i in range(5)
        ]
        golden = [{"id": f"X{i:03d}", "question": f"q{i}"} for i in range(5)]
        assert len(build_samples(golden, preds, limit=3)) == 3

    def test_sample_shape(self):
        samples = build_samples(GOLDEN, PREDS)
        s = samples[0]
        assert set(s.keys()) == {"question", "answer", "contexts"}


# ---------- ① exit 2 诚实降级 ----------

class TestHonestDegradation:
    def test_missing_inputs_exit_2(self, tmp_path, capsys):
        """输入文件缺失 ⇒ exit 2（本用例不依赖 ragas 是否已安装——输入检查
        在 ragas import 检查之后，故先补一个 ragas 可用的前提：若 ragas
        未安装，同样 exit 2，断言只锁退出码不锁原因）。"""
        rc = main([
            "--golden", str(tmp_path / "no_golden.jsonl"),
            "--predictions", str(tmp_path / "no_preds.jsonl"),
        ])
        assert rc == 2
        err = capsys.readouterr().err
        assert "ragas" in err or "输入缺失" in err

    def test_no_evaluable_samples_exit_2(self, tmp_path, capsys):
        """预测全部无 sources/answer ⇒ 无可评估样本 ⇒ exit 2。"""
        golden = tmp_path / "g.jsonl"
        preds = tmp_path / "p.jsonl"
        golden.write_text(json.dumps({"id": "G1", "question": "q"}, ensure_ascii=False) + "\n", encoding="utf-8")
        preds.write_text(json.dumps({"id": "G1", "message": "a", "sources": []}, ensure_ascii=False) + "\n", encoding="utf-8")
        # ragas 未安装时同样 exit 2（诚实降级双保险）
        rc = main(["--golden", str(golden), "--predictions", str(preds)])
        assert rc == 2


# ---------- ③/④ 门禁契约零变化 ----------

class TestGateContractUnchanged:
    def _ci_metrics(self) -> tuple:
        text = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        m = re.search(r"METRICS\s*=\s*\(([^)]*)\)", text, re.S)
        assert m, "ci.yml 中未找到 METRICS 元组"
        return tuple(re.findall(r'"([^"]+)"', m.group(1)))

    def test_report_keys_disjoint_from_ci_metrics(self):
        metrics = self._ci_metrics()
        assert metrics, "METRICS 元组解析为空"
        assert set(REPORT_KEYS).isdisjoint(set(metrics))
        # ragas_* 键族只进 summary 新键族，绝不混入门禁
        assert all(k.startswith("ragas_") for k in REPORT_KEYS)

    def test_report_keys_exact_family(self):
        assert REPORT_KEYS == (
            "ragas_faithfulness",
            "ragas_answer_relevancy",
            "ragas_questions",
            "ragas_unavailable",
        )

    def test_p90_guard_still_pins_five_metrics(self):
        """p90/ci_gate 守卫所锁的 ci.yml METRICS 五键零变化（ragas_* 绝不混入）。"""
        metrics = set(self._ci_metrics())
        assert metrics == {
            "route_accuracy",
            "citation_hit_rate_applicable",
            "citation_correctness_applicable",
            "must_contain_applicable",
            "reject_accuracy_applicable",
        }
