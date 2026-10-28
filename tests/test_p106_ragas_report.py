"""P106：Ragas 忠实度报告（P2-2，observe-only）测试。

覆盖：
① 缺依赖 / 缺 judge 端点 / 缺输入 / 无可评估样本 ⇒ exit 2 诚实降级
② build_samples 纯函数：按 id 对齐取最小 attempt、空 sources/answer 跳过
③ 报告键族固定（REPORT_KEYS）且键族命名收敛（防守卫破）
④ 报告键族与门禁契约零接触（2026-10-26 起：CI 侧 METRICS 元组随
   ``eval-offline`` 机位下线而消失，改由键族本身守住"只进报告、不进门禁"）
"""

from __future__ import annotations

import json

from evals.ragas_report import REPORT_KEYS, build_samples, main


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
#
#: 2026-10-26 起本类少了两条「与 ci.yml METRICS 元组比对」的用例：CI 的 `eval-offline`
#: 质量门禁机位随评测集下线撤除 ⇒ 那份 METRICS 元组不存在了，守卫的前提随前提一起消失
#: （断言一个不存在的集合不包含任何键，是恒真的假守卫）。
#: 「ragas_* 只进报告键族、绝不混入门禁」的纪律仍然成立，改由下面这条**不依赖 CI 文件**
#: 的用例守住：键族命名 + 精确键集本身就是那条纪律的载体。

class TestGateContractUnchanged:
    def test_report_keys_exact_family(self):
        assert REPORT_KEYS == (
            "ragas_faithfulness",
            "ragas_answer_relevancy",
            "ragas_questions",
            "ragas_unavailable",
        )
        # ragas_* 键族只进 summary 新键族，绝不混入门禁
        assert all(k.startswith("ragas_") for k in REPORT_KEYS)
