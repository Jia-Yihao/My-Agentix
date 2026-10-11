"""Unit tests for the heuristic router."""

from agentix.application.agents.router import _heuristic_router, _coerce_to_bool, _ensure_router, ALLOWED_TYPES
from agentix.orchestration.langgraph.lg_states import Router


class TestHeuristicRouter:
    """Tests for _heuristic_router keyword-based fallback routing."""

    def test_returns_none_for_empty_input(self):
        assert _heuristic_router("") is None
        assert _heuristic_router(None) is None

    def test_graphrag_keywords(self):
        keywords = ["法条", "怎么规定", "如何规定", "适用", "案由", "司法解释",
                     "赔偿", "违约", "量刑", "处罚", "责任", "权利义务", "引用", "上位法"]
        for kw in keywords:
            result = _heuristic_router(f"关于{kw}的问题")
            assert result is not None, f"Keyword '{kw}' should trigger graphrag routing"
            assert result.type == "graphrag-query"
            assert "keyword fallback: graphrag" in result.logic

    def test_text2sql_keywords(self):
        keywords = ["统计", "多少", "总数", "数量", "排名"]
        for kw in keywords:
            result = _heuristic_router(f"请问有{kw}部法律")
            assert result is not None, f"Keyword '{kw}' should trigger text2sql routing"
            assert result.type == "text2sql-query"
            assert "keyword fallback: text2sql" in result.logic

    def test_text2sql_takes_priority_over_graphrag(self):
        # "多少" is text2sql, "法条" is graphrag - text2sql should win
        result = _heuristic_router("有多少条法条")
        assert result is not None
        assert result.type == "text2sql-query"

    def test_no_match_returns_none(self):
        result = _heuristic_router("今天天气怎么样")
        assert result is None

    def test_case_insensitive(self):
        result = _heuristic_router("STATISTICS query")
        assert result is None  # English keywords not matched

    def test_preserves_original_question(self):
        q = "民法典的法条怎么规定"
        result = _heuristic_router(q)
        assert result.question == q


class TestCoerceToBool:
    """Tests for _coerce_to_bool utility."""

    def test_none_returns_default(self):
        assert _coerce_to_bool(None) is False
        assert _coerce_to_bool(None, default=True) is True

    def test_string_true_values(self):
        for val in ["1", "true", "True", "TRUE", "yes", "Yes", "y", "Y", "on", "ON"]:
            assert _coerce_to_bool(val) is True, f"'{val}' should be True"

    def test_string_false_values(self):
        for val in ["0", "false", "no", "n", "off", "", "anything"]:
            assert _coerce_to_bool(val) is False, f"'{val}' should be False"

    def test_bool_passthrough(self):
        assert _coerce_to_bool(True) is True
        assert _coerce_to_bool(False) is False

    def test_int_values(self):
        assert _coerce_to_bool(1) is True
        assert _coerce_to_bool(0) is False


class TestEnsureRouter:
    """Tests for _ensure_router compatibility wrapper."""

    def test_router_instance_passthrough(self):
        r = Router(type="kb-query", logic="test", question="q")
        assert _ensure_router(r) is r

    def test_dict_conversion(self):
        r = _ensure_router({"type": "general-query", "logic": "test", "question": "q"})
        assert isinstance(r, Router)
        assert r.type == "general-query"

    def test_invalid_dict_fallback(self):
        r = _ensure_router({"invalid": "data"})
        assert isinstance(r, Router)
        assert r.type == "kb-query"

    def test_none_fallback(self):
        r = _ensure_router(None)
        assert isinstance(r, Router)
        assert r.type == "kb-query"
        assert "missing router" in r.logic


class TestAllowedTypes:
    """Verify all expected route types are registered."""

    def test_all_expected_types_present(self):
        expected = {
            "general-query", "additional-query", "kb-query",
            "graphrag-query", "image-query", "file-query", "text2sql-query",
        }
        assert expected == ALLOWED_TYPES
