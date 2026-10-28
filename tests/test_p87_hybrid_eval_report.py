"""Stage F2b-e（统一执行链 · R-P2-5 混合检索归因报告）契约守卫。

钉死契约（DoD）：

1. **报告键固定**（先例 = ``tests/test_p67_memory_p0_convergence.py`` 对
   ``memory_eval`` 的钉法）：``build_hybrid_report`` 输出键集合恒为
   ``{columns, questions, table, unavailable}``，``columns`` 恒为四通道
   ``{bm25, dense, rrf, rerank}``；
2. **指标口径**：``recall_at_5`` = top-5 内首中题占比（rank>5 记未中）；
   ``mrr`` = 首中位次倒数均值（全列表不截断）；空金标题不进分母
   （``questions`` = 金标非空题数）；四舍五入 4 位；
3. **匹配口径**：法规名归一互相包含 + ``第X条`` 三形（原文/阿拉伯/中文
   数字，``第148条`` ≡ ``第一百四十八条``）；解析失败退化子串（与
   ``run_eval._citation_matches`` 同策略，复用同一组解析器）；
4. **fail-open**：单通道整列跑不通 ⇒ ``unavailable`` + 表内 ``n/a``，
   绝不产出假数；单题通道失败不进该列分母；
5. **门禁边界**：``run_eval``/``quality_gate``/CI ``METRICS`` 五键门零接触
   （源级断言）；live 缺 ``--kb-id``/窗口不足诚实 exit 2，不触达基础设施。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.hybrid_eval import (
    COLUMNS,
    RECALL_K,
    build_hybrid_report,
    doc_matches_gold,
    first_rank,
    load_golden_rows,
    main,
    run_report,
    _int_to_cn,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _Doc:
    def __init__(self, page_content: str = "", **metadata):
        self.page_content = page_content
        self.metadata = metadata


class TestIntToCn:
    @pytest.mark.parametrize(
        "n,expected",
        [
            (0, "零"),
            (5, "五"),
            (10, "十"),
            (15, "十五"),
            (20, "二十"),
            (148, "一百四十八"),
            (577, "五百七十七"),
            (1005, "一千零五"),
            (1050, "一千零五十"),
            (1105, "一千一百零五"),
            (9999, "九千九百九十九"),
            (10000, "10000"),  # 超界诚实退回
        ],
    )
    def test_table(self, n, expected):
        assert _int_to_cn(n) == expected


class TestDocMatching:
    def test_law_only_gold_hits_content(self):
        assert doc_matches_gold(
            "《民法典》", _Doc("民法典 第一编 总则……")
        )
        assert not doc_matches_gold("《民法典》", _Doc("刑法条文"))
        # 法规名仅出现在 metadata 标题（去书名号互相包含）
        assert doc_matches_gold("《民法典》", _Doc("", source="《中华人民共和国民法典》.pdf"))

    def test_arabic_gold_vs_chinese_article_content(self):
        gold = "《民法典》第148条"
        doc = _Doc("民法典 第一百四十八条 债权人可以拒绝……")
        assert doc_matches_gold(gold, doc)

    def test_chinese_gold_vs_arabic_article_content(self):
        gold = "《劳动合同法》第一百四十八条"
        doc = _Doc("劳动合同法 第148条 用人单位……")
        assert doc_matches_gold(gold, doc)

    def test_wrong_article_misses(self):
        gold = "《民法典》第148条"
        doc = _Doc("民法典 第五百七十七条 违约责任")
        assert not doc_matches_gold(gold, doc)

    def test_wrong_law_misses(self):
        gold = "《民法典》第148条"
        doc = _Doc("《刑法》第148条 生产销售伪劣产品")
        assert not doc_matches_gold(gold, doc)

    def test_title_metadata_match_with_full_name(self):
        # 法规名经 _extract_law 归一（去书名号/中华人民共和国 前缀）互相包含
        gold = "《中华人民共和国劳动合同法》第14条"
        doc = _Doc("", source="劳动合同法.pdf", snippet="第十四条 ……")
        assert doc_matches_gold(gold, doc)

    def test_unparsable_gold_falls_back_to_substring(self):
        assert doc_matches_gold("某特殊标识XYZ", _Doc("前缀 某特殊标识XYZ 后缀"))
        assert not doc_matches_gold("某特殊标识XYZ", _Doc("无关文本"))

    def test_empty_doc_never_matches(self):
        assert not doc_matches_gold("《民法典》第148条", _Doc(""))


class TestFirstRank:
    def test_hit_positions(self):
        docs = [_Doc("无关"), _Doc("《民法典》第一百四十八条"), _Doc("x")]
        assert first_rank(docs, ["《民法典》第148条"]) == 2

    def test_miss_returns_none(self):
        docs = [_Doc("无关"), _Doc("还是无关")]
        assert first_rank(docs, ["《民法典》第148条"]) is None

    def test_empty_docs_returns_none(self):
        assert first_rank([], ["《民法典》第148条"]) is None

    def test_any_gold_matches(self):
        docs = [_Doc("《刑法》第二百六十四条 盗窃")]
        assert first_rank(docs, ["《民法典》第148条", "《刑法》第264条"]) == 1


class TestBuildHybridReport:
    def test_fixed_key_sets(self):
        report = build_hybrid_report(
            {c: [1] for c in COLUMNS}, n_questions=3
        )
        assert set(report) == {"columns", "questions", "table", "unavailable"}
        assert set(report["columns"]) == set(COLUMNS)
        assert report["unavailable"] == []
        assert report["questions"] == 3

    def test_metric_math_and_cutoff(self):
        # rank 6：recall@5 记未中，但 MRR 仍计 1/6（全列表口径）
        ranks = {"bm25": [1, 2, None, 6, 3], "dense": [1, 1, 1, 1, 1],
                 "rrf": [None, None, None, None, None],
                 "rerank": [2, 4, 5, 1, None]}
        report = build_hybrid_report(ranks, n_questions=5)
        bm25 = report["columns"]["bm25"]
        assert bm25["recall_at_5"] == round(3 / 5, 4)  # 1,2,3 命中
        assert bm25["mrr"] == round(
            (1.0 + 0.5 + 0.0 + 1 / 6 + 1 / 3) / 5, 4
        )
        assert report["columns"]["dense"]["recall_at_5"] == 1.0
        assert report["columns"]["rrf"]["recall_at_5"] == 0.0
        assert report["columns"]["rrf"]["mrr"] == 0.0
        assert report["columns"]["rerank"]["applicable"] == 5

    def test_unavailable_column_honest(self):
        report = build_hybrid_report(
            {"bm25": [1], "dense": [1], "rrf": [], "rerank": [None]},
            n_questions=1,
        )
        assert report["unavailable"] == ["rrf"]
        assert report["columns"]["rrf"] == {
            "recall_at_5": None,
            "mrr": None,
            "applicable": 0,
        }
        assert "| RRF | n/a | n/a | 0 |" in report["table"]

    def test_markdown_table_four_columns(self):
        report = build_hybrid_report({c: [1] for c in COLUMNS}, n_questions=2)
        table = report["table"]
        for label in ("BM25", "Dense", "RRF", "+Rerank"):
            assert f"| {label} |" in table
        assert f"Recall@{RECALL_K}" in table


class TestRunReport:
    @pytest.mark.asyncio
    async def test_end_to_end_with_injected_runner(self):
        rows = [
            {"id": "A", "question": "q1", "expected_sources": ["《民法典》第148条"]},
            {"id": "B", "question": "q2", "expected_sources": ["《刑法》第264条"]},
            # 空金标题：不进分母（questions=2）
            {"id": "C", "question": "q3", "expected_sources": []},
        ]

        async def runner(question):
            if question == "q1":
                return {
                    "bm25": [_Doc("《民法典》第一百四十八条")],
                    "dense": [_Doc("无关")],
                    "rrf": [_Doc("无关"), _Doc("《民法典》第148条")],
                    "rerank": None,  # 单题通道失败：不进该列分母
                }
            return {
                "bm25": [_Doc("无关")],
                "dense": [_Doc("《刑法》第二百六十四条")],
                "rrf": [_Doc("《刑法》第264条")],
                "rerank": [_Doc("《刑法》第264条")],
            }

        report = await run_report(rows=rows, runner=runner)
        assert report["questions"] == 2
        cols = report["columns"]
        assert cols["bm25"] == {"recall_at_5": 0.5, "mrr": 0.5, "applicable": 2}
        assert cols["dense"] == {"recall_at_5": 0.5, "mrr": 0.5, "applicable": 2}
        assert cols["rrf"] == {"recall_at_5": 1.0, "mrr": 0.75, "applicable": 2}
        # rerank 只有 q2 跑通（q1 None 被剔出分母）
        assert cols["rerank"] == {"recall_at_5": 1.0, "mrr": 1.0, "applicable": 1}
        assert report["unavailable"] == []


class TestLoadGolden:
    # `test_default_kb_small_set` 随评测集于 2026-10-26 下线：它读的是已删除的
    # ``evals/legal_golden.jsonl``，断言的是「那份数据集里有 23 问 kb 小集」这件事。
    # 下面的 `test_route_filter_and_limit` 用内联 fixture 覆盖了同一个函数
    # （route 过滤 + 空 expected_sources 剔除），代码侧覆盖面没有丢。

    def test_route_filter_and_limit(self, tmp_path: Path):
        path = tmp_path / "g.jsonl"
        payload = [
            {"id": "1", "route_type": "kb-query", "expected_sources": ["a"]},
            {"id": "2", "route_type": "graphrag", "expected_sources": ["b"]},
            {"id": "3", "route_type": "kb-query", "expected_sources": []},
            {"id": "4", "route_type": "kb-query", "expected_sources": ["c"]},
        ]
        path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in payload),
            encoding="utf-8",
        )
        rows = load_golden_rows(path, limit=40, route="kb-query")
        assert [r["id"] for r in rows] == ["1", "4"]  # route 过滤 + 空金标过滤
        rows_any = load_golden_rows(path, limit=1, route="any")
        assert [r["id"] for r in rows_any] == ["1"]


class TestCliHonestExits:
    def test_missing_kb_id_exits_2_without_infra(self, capsys):
        assert main([]) == 2
        assert main(["--kb-id", "  "]) == 2
        err = capsys.readouterr().err
        assert "--kb-id" in err

    def test_search_k_below_window_exits_2(self, capsys):
        assert main(["--kb-id", "KB", "--search-k", str(RECALL_K - 1)]) == 2
        assert str(RECALL_K) in capsys.readouterr().err

    def test_parser_help_smoke(self):
        from evals.hybrid_eval import build_parser

        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["--help"])
        assert exc.value.code == 0


class TestGateBoundary:
    """只增报告键，不动质量门禁（源级钉死）。"""

    @staticmethod
    def _read(rel: str) -> str:
        with open(
            PROJECT_ROOT / rel, encoding="utf-8", newline=""
        ) as fh:
            return fh.read()

    def test_run_eval_not_touched(self):
        src = self._read("evals/run_eval.py")
        assert "hybrid_eval" not in src

    def test_quality_gate_not_touched(self):
        src = self._read("evals/quality_gate.py")
        assert "hybrid_eval" not in src

    def test_ci_metrics_not_touched(self):
        src = self._read(".github/workflows/ci.yml")
        assert "hybrid_eval" not in src
        assert "recall_at" not in src
        assert "mrr" not in src.lower()

    def test_documented_in_evals_readme(self):
        src = self._read("evals/README.md")
        assert "hybrid_eval" in src
