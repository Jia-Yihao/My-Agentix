"""检索侧确定性槽位提取的契约测试。

覆盖 5W1H 面判定、三类加法扩展、原句保留与上限，以及开关的三处同步。
"""

from __future__ import annotations

from pathlib import Path

from agentix.retrieval.usecases import chat_service
from agentix.retrieval.usecases import query_slots as slots


class TestFacetDetection:
    def test_fixed_order(self):
        result = slots.detect_facets("谁在什么情况下、多久之内、依据什么、怎么处理")
        assert result == ("WHO", "WHAT", "WHEN", "WHY", "HOW")

    def test_no_cue_returns_empty(self):
        assert slots.detect_facets("《民法典》第148条") == ()

    def test_all_six_facets(self):
        result = slots.detect_facets("谁 哪些 多久 哪里 为什么 怎么")
        assert result == slots.FACET_ORDER


class TestAdditiveExpansion:
    def test_original_is_always_prefix(self):
        q = "《婚姻法》规定的离婚法定条件是什么？"
        result = slots.expand_query(q)
        assert result.search_text.startswith(q)
        assert result.original == q

    def test_superseded_law_maps_to_current_name_only(self):
        result = slots.expand_query("《合同法》第107条关于违约责任是怎么规定的？")
        assert result.laws == ("民法典",)
        assert "民法典" in result.added_terms
        assert "第577条" not in result.added_terms

    def test_longest_law_name_wins(self):
        result = slots.expand_query("《民法通则》关于诉讼时效的规定现在还能用吗？")
        assert result.laws == ("民法典",)
        assert result.added_terms.count("民法典") == 1

    def test_chinese_article_number_gets_arabic_form(self):
        result = slots.expand_query("《民法典》第一百四十八条讲了什么")
        assert result.numbers == ("第148条",)
        assert "第148条" in result.search_text

    def test_arabic_article_number_is_not_duplicated(self):
        result = slots.expand_query("《民法典》第148条讲了什么")
        assert result.numbers == ()
        assert "第148条" not in result.added_terms

    def test_colloquial_phrase_adds_policy_term(self):
        result = slots.expand_query("一方违约后损失怎么赔？")
        assert "赔偿损失" in result.added_terms
        assert result.facets == ("HOW",)

    def test_existing_term_is_not_added_again(self):
        result = slots.expand_query("赔偿损失怎么赔？")
        assert "赔偿损失" not in result.added_terms

    def test_terms_are_unique_and_bounded(self):
        q = "怎么赔 赔钱 赔多少 请求撤销 谁来担责 算工伤 加班费 几倍赔偿 过了时效 真实意思"
        result = slots.expand_query(q)
        assert len(result.added_terms) == len(set(result.added_terms))
        assert len(result.added_terms) <= slots.MAX_TERMS

    def test_max_terms_is_clamped_to_documented_cap(self):
        result = slots.expand_query("怎么赔 请求撤销", max_terms=999)
        assert len(result.added_terms) <= slots.MAX_TERMS

    def test_empty_input_stays_empty(self):
        result = slots.expand_query("")
        assert result.search_text == ""
        assert result.added_terms == ()

    def test_pure_function(self):
        q = "《物权法》上的善意取得，谁来担责，怎么赔？"
        assert slots.expand_query(q) == slots.expand_query(q)


class TestWiring:
    def test_flag_defaults_off(self):
        from agentix.retrieval.settings import settings as rset_settings

        assert rset_settings.RETRIEVAL_SLOT_EXPANSION_ENABLED is False

    def test_env_example_documents_key(self):
        text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
        assert "# RETRIEVAL_SLOT_EXPANSION_ENABLED=false" in text

    def test_chat_service_reads_flag_in_single_seam(self):
        text = Path(chat_service.__file__).read_text(encoding="utf-8")
        assert "settings.RETRIEVAL_SLOT_EXPANSION_ENABLED" in text
        assert 'cache_suffix += "|s1"' in text
