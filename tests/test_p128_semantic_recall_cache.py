"""E7 · 召回级语义结果缓存的守卫。

验收口径（"相似问题命中用例 + 「不相似必须不命中」反例；默认关下零行为变化"）
----------------------------------------------------------------------------------------------
1. **默认关 ⇒ 逐位等价**（工程约定 1）：flag 关时 ``semantic_recall_peek`` 必须在
   **构造 embeddings 之前**就返回 ``None``。这句话的守卫**不是** raise 桩：raise 会被本模块的
   fail-open 咽成 ``None``，"flag 关"与"端点挂了"在断言眼里于是长得一模一样——变异实测 M1
   第一轮全绿就是这么发现的（见 CHANGELOG 的 P5 条）。所以 peek 侧数**构造次数**
   （``built == []`` 且 ``embeddings.queries == []``，断言的是"没发生"），只有链路侧用 raise 桩
   （把 ``semantic_recall_peek``／``semantic_remember`` 两个名字换成"一调用就 raise"）——
   那一段没有 fail-open 包装，raise 不会被吞。**两边判据不同，是因为两边的失败形态不同。**
2. **反例必须真反**：只测"相似命中"的守卫等于没测 τ。本文件用一对**精确到浮点可表示**的
   向量做边界（``[4,3]`` vs ``[3,4]`` 的余弦 = 24/25 = 0.96，与字面量 ``0.96`` 是同一个
   double），因此 "τ=0.96 命中 / τ=0.97 不命中" 是确定性断言，不依赖容差。
3. **权限隔离是结构性的**：语义 bucket 与精确键必须由**同一个** ``_retrieval_cache_key``
   派生（约定 6 ④）。这条有专门用例比对两个键的差分——第二个隔离源出错的表现形式是
   跨权限命中，不是报错，所以不能靠"看起来带了租户"来保证。

覆盖面：判定面纯函数（含 τ 边界与四类必须不命中的反例）/ 存储面（TTL、去重刷新、有界
逐出、拒写）/ 配置面（三键默认值、单配置面、非法值不以 flag 为前提）/ 观测面（零新 label、
打点吞错）/ embeddings 复用与 fail-open / peek+remember 端到端 / ``chat_service`` 接线
（**AST 结构守卫**，不用子串——理由见 §7 注释）/ 真链路行为等价 / 文档同步面。
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import logging
import threading
from pathlib import Path

import pytest
from langchain_core.documents import Document

from agentix.retrieval.infra.rag import semantic_cache as sc
from agentix.retrieval.infra.rag.semantic_cache import (
    CACHE_TYPE,
    DEFAULT_TAU,
    DEFAULT_TTL_SECONDS,
    MAX_ENTRIES_PER_BUCKET,
    SemanticPeek,
    best_match,
    semantic_remember,
    semantic_recall_peek,
    semantic_recall_stats,
    SemanticRecallCache,
)
from agentix.retrieval.settings import settings as rs
from agentix.retrieval.usecases import chat_service as cs

REPO = Path(__file__).resolve().parent.parent
SEMANTIC_PY = REPO / "agentix" / "retrieval" / "infra" / "rag" / "semantic_cache.py"
CHAT_SERVICE_PY = REPO / "agentix" / "retrieval" / "usecases" / "chat_service.py"
CLIENT_PY = REPO / "agentix" / "retrieval" / "infra" / "embedding" / "client.py"

_SEMANTIC_KEYS = (
    "SEMANTIC_CACHE_ENABLED",
    "SEMANTIC_CACHE_TAU",
    "SEMANTIC_CACHE_TTL_SECONDS",
)

#: 一对"像但不同"的问法向量：cosine([4,3],[3,4]) = 24/25 = 0.96（浮点可精确表示）。
_NEAR_A = [4.0, 3.0]
_NEAR_B = [3.0, 4.0]
#: 与 _NEAR_A 的余弦 = 4/5 = 0.8 ⇒ 在 0.95 下必须不命中。
_FAR = [1.0, 0.0]


@pytest.fixture(autouse=True)
def _isolate_cache_and_embeddings(monkeypatch):
    """每个用例都从空缓存 + 无 embeddings 实例开始。

    ``_CACHE`` 与 ``_EMBEDDINGS_INSTANCE`` 是进程内单例，不清就等于把上一条用例的
    向量与条目带进下一条——那种失败是随机的、按执行顺序变的，最难查。
    """
    sc.reset_semantic_recall_cache()
    sc.reset_embeddings_holder()
    monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", False)
    monkeypatch.setattr(rs, "SEMANTIC_CACHE_TAU", DEFAULT_TAU)
    monkeypatch.setattr(rs, "SEMANTIC_CACHE_TTL_SECONDS", DEFAULT_TTL_SECONDS)
    yield
    sc.reset_semantic_recall_cache()
    sc.reset_embeddings_holder()


class _FakeEmbeddings:
    """按问法查表的假后端：``model`` 必须是实例属性，bucket 靠它区分向量空间。"""

    def __init__(self, table: dict[str, list[float]], *, model: str = "fake-m3", fail=False):
        self.model = model
        self._table = table
        self._fail = fail
        self.queries: list[str] = []

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        if self._fail:
            raise RuntimeError("embedding endpoint down")
        if text not in self._table:
            raise AssertionError(f"unexpected query embedded: {text!r}")
        return list(self._table[text])

    def embed_documents(self, texts):
        return [self.embed_query(t) for t in texts]


def _install_embeddings(monkeypatch, embeddings: _FakeEmbeddings) -> list:
    """把 ``create_embeddings`` 换成返回固定实例的桩，并记录被构造了几次。

    必须桩在 ``client`` 模块的属性上：``semantic_cache._embeddings_or_none()`` 是
    **运行期导入**（避免 import 环），只有替换源模块才拦得住。
    """
    built: list = []

    def _factory(config=None):
        built.append(config)
        return embeddings

    import agentix.retrieval.infra.embedding.client as client_module

    monkeypatch.setattr(client_module, "create_embeddings", _factory)
    return built


def _install_counting(monkeypatch, embeddings: _FakeEmbeddings, built: list) -> None:
    """``_install_embeddings`` 的计数版：断言"没构造"比断言"没报错"更强（见 M1 教训）。"""
    import agentix.retrieval.infra.embedding.client as client_module

    def _factory(config=None):
        built.append(config)
        return embeddings

    monkeypatch.setattr(client_module, "create_embeddings", _factory)


def _payload(text: str) -> str:
    return cs._serialize_docs_for_cache([Document(page_content=text, metadata={"chunk_id": text})])


# ===========================================================================
# 1. 判定面：best_match（τ 的唯一比较点）
# ===========================================================================


class TestBestMatch:
    def test_similar_pair_hits_at_boundary_tau(self):
        """τ == 相似度 ⇒ 命中（判据是 ``score < tau`` 才跳过，边界归"够像"）。"""
        out = best_match(_NEAR_A, [(_NEAR_B, 100.0)], tau=0.96)
        assert out is not None
        index, score = out
        assert index == 0
        assert score == pytest.approx(0.96, abs=1e-12)

    def test_dissimilar_pair_must_not_hit_at_just_above_similarity(self):
        """反例：τ 抬高 0.01 就不许命中。没有这条，τ 写错成 0 也能让命中用例全绿。"""
        assert best_match(_NEAR_A, [(_NEAR_B, 100.0)], tau=0.97) is None

    def test_orthogonal_vector_misses_at_default_tau(self):
        assert best_match(_NEAR_A, [(_FAR, 100.0)], tau=DEFAULT_TAU) is None

    def test_dimension_mismatch_never_hits(self):
        """维度不符 ⇒ 复用 memory_recall 的 0.0 语义：换模型/被截断的向量不许命中。"""
        assert best_match([1.0, 2.0, 3.0], [([1.0, 2.0], 100.0)], tau=0.5) is None

    def test_negative_and_zero_vectors_never_hit(self):
        assert best_match([1.0, 0.0], [([-1.0, 0.0], 100.0)], tau=0.5) is None
        assert best_match([1.0, 0.0], [([0.0, 0.0], 100.0)], tau=0.5) is None

    def test_non_positive_tau_matches_nothing(self):
        """``tau<=0`` 直接不判——它同时是"任何问法都算命中"的形态，配置面也会拒绝，
        但判定面自己不放手（纵深防御：判据与校验分家时，判据宁可保守）。"""
        assert best_match(_NEAR_A, [(_NEAR_A, 100.0)], tau=0.0) is None
        assert best_match(_NEAR_A, [(_NEAR_A, 100.0)], tau=-1.0) is None

    def test_empty_inputs_short_circuit(self):
        assert best_match([], [(_NEAR_A, 1.0)], tau=0.5) is None
        assert best_match(_NEAR_A, [], tau=0.5) is None

    def test_tie_break_prefers_fresher_entry(self):
        """同分取更新鲜的那条：同分=两条问法一样近，新鲜的那条更可能反映当前索引。"""
        candidates = [(_NEAR_B, 10.0), (_NEAR_B, 90.0)]
        index, _score = best_match(_NEAR_A, candidates, tau=0.9, now=100.0)
        assert index == 1

    def test_highest_score_wins_over_fresher_but_closer_to_nothing(self):
        candidates = [([0.98, 0.17], 10.0), (_NEAR_B, 90.0)]
        index, score = best_match([1.0, 0.0], candidates, tau=0.5, now=100.0)
        assert index == 0
        assert score > 0.98

    def test_best_match_is_pure_and_reads_no_settings(self):
        """约定 6 ④：τ 只有一个源。判定函数不许自己摸 settings，否则"命中"会有两本账。"""
        tree = ast.parse(SEMANTIC_PY.read_text(encoding="utf-8"))
        fn = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "best_match"
        )
        referenced = {
            node.attr
            for node in ast.walk(fn)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        }
        assert "SEMANTIC_CACHE_TAU" not in referenced
        assert not any(
            isinstance(node, ast.ImportFrom) and "settings" in (node.module or "")
            for node in ast.walk(fn)
        )
        assert "settings" not in {node.id for node in ast.walk(fn) if isinstance(node, ast.Name)}


# ===========================================================================
# 2. 存储面：SemanticRecallCache（判定委托给 best_match）
# ===========================================================================


class TestStorageSurface:
    def test_store_rejects_empty_or_non_positive_inputs(self):
        cache = SemanticRecallCache()
        assert cache.store("", _NEAR_A, "x", ttl_seconds=120, now=0.0) is False
        assert cache.store("b", [], "x", ttl_seconds=120, now=0.0) is False
        assert cache.store("b", _NEAR_A, "", ttl_seconds=120, now=0.0) is False
        assert cache.store("b", _NEAR_A, "   ", ttl_seconds=120, now=0.0) is False
        assert cache.store("b", _NEAR_A, "x", ttl_seconds=0, now=0.0) is False
        assert cache.store("b", _NEAR_A, "x", ttl_seconds=-5, now=0.0) is False
        assert cache.stats()["entries"] == 0, "拒写却仍留下条目 = 空载荷进缓存"

    def test_lookup_requires_bucket_vector_and_positive_window(self):
        cache = SemanticRecallCache()
        cache.store("b", _NEAR_A, "x", ttl_seconds=120, now=0.0)
        assert cache.lookup("", _NEAR_A, tau=0.5, ttl_seconds=120, now=0.0) is None
        assert cache.lookup("b", [], tau=0.5, ttl_seconds=120, now=0.0) is None
        assert cache.lookup("b", _NEAR_A, tau=0.0, ttl_seconds=120, now=0.0) is None
        assert cache.lookup("b", _NEAR_A, tau=0.5, ttl_seconds=0, now=0.0) is None
        assert cache.lookup("other", _NEAR_A, tau=0.5, ttl_seconds=120, now=0.0) is None

    def test_entry_expires_at_read_time(self):
        cache = SemanticRecallCache()
        cache.store("b", _NEAR_A, "x", ttl_seconds=120, now=0.0)
        assert cache.lookup("b", _NEAR_A, tau=0.99, ttl_seconds=120, now=119.0) is not None
        assert cache.lookup("b", _NEAR_A, tau=0.99, ttl_seconds=120, now=121.0) is None

    def test_ttl_change_takes_effect_immediately_not_after_old_window_drains(self):
        """条目只存写入时刻 ⇒ 运维把 TTL 改短是"从现在起更短"，不需要等旧条目漂完。"""
        cache = SemanticRecallCache()
        cache.store("b", _NEAR_A, "x", ttl_seconds=120, now=0.0)
        # 同一条目、同一时刻：按出厂窗口还算新鲜，按新的短窗口已经过期
        assert cache.lookup("b", _NEAR_A, tau=0.99, ttl_seconds=120, now=50.0) is not None
        assert cache.lookup("b", _NEAR_A, tau=0.99, ttl_seconds=10, now=50.0) is None

    def test_expired_entries_are_pruned_on_read(self):
        cache = SemanticRecallCache()
        cache.store("b", _NEAR_A, "x", ttl_seconds=10, now=0.0)
        assert cache.lookup("b", _NEAR_A, tau=0.99, ttl_seconds=10, now=99.0) is None
        assert cache.stats()["buckets"] == 0, "整桶过期还留着 = 长尾桶只增不减"

    def test_identical_vector_refreshes_instead_of_stacking(self):
        cache = SemanticRecallCache()
        cache.store("b", _NEAR_A, "old", ttl_seconds=120, now=0.0)
        cache.store("b", _NEAR_A, "new", ttl_seconds=120, now=100.0)
        assert cache.stats()["entries"] == 1
        match = cache.lookup("b", _NEAR_A, tau=0.99, ttl_seconds=120, now=150.0)
        assert match is not None and match.payload == "new"

    def test_near_duplicates_are_kept_as_separate_entries(self):
        """近似但不逐位相同 ⇒ 各留一条：它们是"不同问法的答案"，合并等于提前宣布二者等价。"""
        cache = SemanticRecallCache()
        cache.store("b", _NEAR_A, "a-payload", ttl_seconds=120, now=0.0)
        cache.store("b", _NEAR_B, "b-payload", ttl_seconds=120, now=1.0)
        assert cache.stats()["entries"] == 2

    def test_bucket_is_bounded_and_evicts_oldest_first(self):
        cache = SemanticRecallCache(max_entries_per_bucket=3)
        for i in range(6):
            vector = [float(i + 1), 1.0]  # 逐条不同，够不够像是另一回事，这里只数容量
            assert cache.store("b", vector, f"p{i}", ttl_seconds=120, now=float(i))
        assert cache.stats()["entries"] == 3
        # 白盒看剩下的是谁：FIFO 逐出必须逐的是**最旧**的，不是随机一条
        survivors = [row[1] for row in cache._buckets["b"]]
        assert survivors == ["p3", "p4", "p5"]

    def test_default_bucket_is_bounded_by_the_shipped_constant(self):
        cache = SemanticRecallCache()
        for i in range(MAX_ENTRIES_PER_BUCKET + 10):
            cache.store("b", [float(i + 1), 1.0], f"p{i}", ttl_seconds=120, now=float(i))
        assert cache.stats()["entries"] == MAX_ENTRIES_PER_BUCKET

    def test_default_bound_is_a_bounded_constant(self):
        assert MAX_ENTRIES_PER_BUCKET == 64
        assert SemanticRecallCache().stats()["max_entries_per_bucket"] == MAX_ENTRIES_PER_BUCKET

    def test_lookup_returns_a_fresh_immutable_match_each_time(self):
        """命中给的是新建的 frozen dataclass + str 载荷 ⇒ 调用方改不动缓存内容。"""
        cache = SemanticRecallCache()
        cache.store("b", _NEAR_A, _payload("doc"), ttl_seconds=120, now=0.0)
        first = cache.lookup("b", _NEAR_A, tau=0.99, ttl_seconds=120, now=1.0)
        second = cache.lookup("b", _NEAR_A, tau=0.99, ttl_seconds=120, now=2.0)
        assert first is not second
        assert first.payload == second.payload
        assert first.age_seconds < second.age_seconds
        with pytest.raises(dataclasses.FrozenInstanceError):
            first.payload = "tampered"  # type: ignore[misc]

    def test_int_and_float_vectors_are_the_same_question(self):
        """向量一律按 float 收（后端可能回 int/Decimal）+ 逐位相同判据也按 float 比。

        不收口的话 ``[4, 3]`` 与 ``[4.0, 3.0]`` 会被当成两条不同问法各占一格，
        64 格上限就会被同一句话的两种数值伪装填满。
        """
        cache = SemanticRecallCache()
        assert cache.store("b", [4, 3], "int型", ttl_seconds=120, now=0.0) is True
        assert cache.store("b", [4.0, 3.0], "float型", ttl_seconds=120, now=1.0) is True
        assert cache.stats()["entries"] == 1, "同一向量的两种数值形态各占一格"
        row = cache._buckets["b"][0][0]
        assert all(isinstance(x, float) for x in row)

    def test_vectors_of_different_length_are_not_treated_as_identical(self):
        """逐位相同判据必须先看长度：只 zip 短边会让 ``[1,2]`` 与 ``[1,2,3]`` 判为同一条，
        于是**较长的向量被丢掉**、缓存里剩下一个空间都对不上的条目。"""
        cache = SemanticRecallCache()
        cache.store("b", [1.0, 2.0], "二维", ttl_seconds=120, now=0.0)
        cache.store("b", [1.0, 2.0, 3.0], "三维", ttl_seconds=120, now=1.0)
        assert cache.stats()["entries"] == 2
        assert [row[1] for row in cache._buckets["b"]] == ["二维", "三维"]

    def test_clear_resets_everything(self):
        cache = SemanticRecallCache()
        cache.store("b", _NEAR_A, "x", ttl_seconds=120, now=0.0)
        cache.clear()
        assert cache.stats() == {"buckets": 0, "entries": 0, "max_entries_per_bucket": 64}


# ===========================================================================
# 3. 配置面：三键默认关 + 只活在一个面 + 非法值不以 flag 为前提
# ===========================================================================


class TestConfigSurface:
    def test_three_keys_ship_disabled_with_conservative_values(self):
        source = (REPO / "agentix" / "retrieval" / "settings.py").read_text(encoding="utf-8")
        assert '_bool_env("SEMANTIC_CACHE_ENABLED", "false")' in source
        assert 'os.getenv("SEMANTIC_CACHE_TAU", "0.95")' in source
        assert 'os.getenv("SEMANTIC_CACHE_TTL_SECONDS", "120")' in source
        assert rs.SEMANTIC_CACHE_ENABLED is False
        assert DEFAULT_TAU == 0.95 and DEFAULT_TTL_SECONDS == 120

    def test_ttl_is_shorter_than_the_exact_key_window(self):
        """语义键复用的是"另一句话"的结果 ⇒ 必须比精确键更快失效（路线图风险段）。"""
        exact = int(getattr(rs, "RETRIEVAL_CACHE_TTL_SECONDS", 600) or 600)
        assert DEFAULT_TTL_SECONDS * 4 <= exact

    def test_the_keys_live_on_exactly_one_settings_surface(self):
        unified = (REPO / "agentix" / "config" / "settings.py").read_text(encoding="utf-8")
        retrieval = (REPO / "agentix" / "retrieval" / "settings.py").read_text(encoding="utf-8")
        for key in _SEMANTIC_KEYS:
            assert key not in unified, f"{key} 同时出现在统一配置层 ⇒ 两个源（约定 6 ④）"
            assert key in retrieval
        assert "from agentix.retrieval.settings import settings" in SEMANTIC_PY.read_text(
            encoding="utf-8"
        )

    def test_validation_rejects_bad_values_without_requiring_the_flag(self):
        from agentix.retrieval.settings import validate_app_settings

        originals = (rs.SEMANTIC_CACHE_TAU, rs.SEMANTIC_CACHE_TTL_SECONDS)
        try:
            for bad_tau in (0.0, -1.0, 1.5):
                rs.SEMANTIC_CACHE_TAU = bad_tau
                errors, _warnings = validate_app_settings()
                assert any("SEMANTIC_CACHE_TAU" in e for e in errors), f"τ={bad_tau} 竟被放过"
            rs.SEMANTIC_CACHE_TAU = DEFAULT_TAU
            rs.SEMANTIC_CACHE_TTL_SECONDS = 0
            errors, _warnings = validate_app_settings()
            assert any("SEMANTIC_CACHE_TTL_SECONDS" in e for e in errors)
        finally:
            rs.SEMANTIC_CACHE_TAU, rs.SEMANTIC_CACHE_TTL_SECONDS = originals

    def test_validation_passes_on_the_shipped_defaults(self):
        from agentix.retrieval.settings import validate_app_settings

        errors, _warnings = validate_app_settings()
        assert not [e for e in errors if "SEMANTIC_CACHE" in e]

    def test_settings_are_read_only_inside_the_config_helper(self):
        """口径 2 的存储侧对应物：全模块只有 ``_config()`` 摸 settings。

        散落的 ``getattr(settings, ...)`` 会让"运行期改 TTL 生不生效"这类问题没有单一答案。
        """
        tree = ast.parse(SEMANTIC_PY.read_text(encoding="utf-8"))
        readers: list[str] = []
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            imports_settings = any(
                isinstance(node, ast.ImportFrom)
                and (node.module or "").endswith("retrieval.settings")
                for node in ast.walk(fn)
            )
            names_settings = any(
                isinstance(node, ast.Name) and node.id == "settings" for node in ast.walk(fn)
            )
            if imports_settings or names_settings:
                readers.append(fn.name)
        assert readers == ["_config"], f"配置读取点必须唯一，实际：{readers}"


# ===========================================================================
# 4. 观测面：零新 label + 打点失败不炸主链路
# ===========================================================================


class TestObservability:
    def test_cache_type_value_is_bounded_and_distinct_from_answer_level(self):
        assert CACHE_TYPE == "semantic_recall"
        assert CACHE_TYPE.isascii() and CACHE_TYPE.replace("_", "").isalnum()
        # 既有取值：答案级未接线类用 "semantic"、精确键用 "retrieval"、内容缓存用 "embedding"
        assert CACHE_TYPE not in {"semantic", "retrieval", "embedding", "prompt_prefix"}

    def test_module_declares_no_new_metric_label_names(self):
        """约定 3：新 label 名必须先进 p100 的 ALLOWED_METRIC_LABELS。本批只复用既有名。"""
        tree = ast.parse(SEMANTIC_PY.read_text(encoding="utf-8"))
        used: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "labels":
                used.update(kw.arg for kw in node.keywords if isinstance(kw.arg, str))
        assert used <= {
            "cache_type",
            "component",
        }, f"出现未登记的 label 名：{used - {'cache_type', 'component'}}"

    def test_hit_and_miss_go_to_the_existing_cache_series(self, monkeypatch):
        import agentix.observability.metrics as m

        seen: list[tuple[str, str]] = []
        monkeypatch.setattr(m, "record_cache_hit", lambda t: seen.append(("hit", t)))
        monkeypatch.setattr(m, "record_cache_miss", lambda t: seen.append(("miss", t)))
        embeddings = _FakeEmbeddings({"q": _NEAR_A, "q2": _NEAR_B})
        _install_embeddings(monkeypatch, embeddings)
        monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", True)

        first = semantic_recall_peek("scope", "q")
        assert first is not None and first.match is None, "空缓存里的首次查询必须是一次真 miss"
        assert semantic_remember(first, _payload("第188条")) is True
        second = semantic_recall_peek("scope", "q2")
        assert second is not None and second.hit
        assert seen == [("miss", CACHE_TYPE), ("hit", CACHE_TYPE)]

    def test_metric_failure_never_breaks_the_cache_path(self, monkeypatch):
        """R-P0-5 同口径：埋点炸了也不许把一次成功检索打成 500。"""
        import agentix.observability.metrics as m

        def _boom(_t):
            raise RuntimeError("prometheus registry down")

        monkeypatch.setattr(m, "record_cache_hit", _boom)
        monkeypatch.setattr(m, "record_cache_miss", _boom)
        embeddings = _FakeEmbeddings({"q": _NEAR_A})
        _install_embeddings(monkeypatch, embeddings)
        monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", True)
        peek = semantic_recall_peek("scope", "q")
        assert peek is not None and peek.match is None

    def test_degraded_uses_existing_component_label_and_warns(self, monkeypatch, caplog):
        import agentix.observability.metrics as m

        recorded: list[str] = []
        monkeypatch.setattr(m, "record_degraded", lambda c: recorded.append(c))
        monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", True)
        _install_embeddings(monkeypatch, _FakeEmbeddings({}, fail=True))
        with caplog.at_level(logging.WARNING):
            assert semantic_recall_peek("scope", "q") is None
        assert recorded == [sc.DEGRADED_COMPONENT]
        assert "embedding unavailable" in caplog.text


# ===========================================================================
# 5. embeddings 复用与 fail-open
# ===========================================================================


class TestEmbeddingsAndFailOpen:
    def test_flag_off_returns_none_before_any_embedding_is_constructed(self, monkeypatch):
        """约定 1 的可证伪版本：flag 关 ⇒ 连客户端都不该构造，更不该发请求。

        这里**不能**用"一调用就 raise"的桩：``peek`` 的 fail-open 会把 raise 咽成
        ``None``，于是"flag 关"与"embedding 挂了"两种结局长得一模一样，变异实测
        （M1：把 ``if not config["enabled"] …: return None`` 改成 ``if False:``）当场全绿。
        要证伪"不构造"，只能**数构造次数**——断言的是没发生，不是没报错。
        """
        built: list = []
        embeddings = _FakeEmbeddings({"q": _NEAR_A})
        _install_counting(monkeypatch, embeddings, built)
        monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", False)
        assert semantic_recall_peek("scope", "q") is None
        assert built == [], f"flag 关却构造了 {len(built)} 次 embeddings ⇒ 默认关不等价"
        assert embeddings.queries == [], "flag 关却发了 embedding 请求"
        assert semantic_remember(None, _payload("x")) is False

    def test_blank_scope_or_blank_query_is_not_a_verdict(self, monkeypatch):
        """空输入同样要"没构造、没发请求"，而不是"构造了但返回 None"。"""
        built: list = []
        embeddings = _FakeEmbeddings({"q": _NEAR_A})
        _install_counting(monkeypatch, embeddings, built)
        monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", True)
        assert semantic_recall_peek("", "q") is None
        assert semantic_recall_peek("scope", "") is None
        assert semantic_recall_peek("scope", "   ") is None
        assert built == [] and embeddings.queries == []

    def test_embedding_failure_degrades_to_no_verdict_not_an_error(self, monkeypatch):
        _install_embeddings(monkeypatch, _FakeEmbeddings({}, fail=True))
        monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", True)
        assert semantic_recall_peek("scope", "q") is None

    def test_embeddings_instance_is_reused_until_the_signature_changes(self, monkeypatch):
        """README §15.2 口径③：构造一次要 16–24ms（另一套 SDK 栈上 2–4s）。
        一个"省延迟"的缓存每次查询先付一次构造费，就只在命中时才划算。"""
        first = _FakeEmbeddings({"q": _NEAR_A}, model="m-A")
        built = _install_embeddings(monkeypatch, first)
        monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", True)
        semantic_recall_peek("scope", "q")
        semantic_recall_peek("scope", "q")
        assert len(built) == 1, "同一 (model, base_url) 下反复构造 = 把成本加回主链路"

        import agentix.retrieval.config as cfg_module

        real_get_default = cfg_module.get_default_config

        def _other_endpoint():
            out = dict(real_get_default())
            out["embedding_base_url"] = "http://127.0.0.1:9999/v1"
            return out

        monkeypatch.setattr(cfg_module, "get_default_config", _other_endpoint)
        second = _FakeEmbeddings({"q": _NEAR_A}, model="m-B")
        monkeypatch.setattr(
            "agentix.retrieval.infra.embedding.client.create_embeddings",
            lambda config=None: (built.append(config), second)[1],
        )
        assert sc._embeddings_or_none() is second, "换了端点还复用旧实例 = 拿旧客户端算新向量"

    def test_remember_refuses_none_empty_and_resultless_payloads(self, monkeypatch):
        monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", True)
        peek = SemanticPeek(bucket="b", query="q", vector=tuple(_NEAR_A), match=None)
        assert semantic_remember(None, _payload("x")) is False
        assert semantic_remember(peek, "") is False
        assert semantic_remember(peek, "[]") is False
        assert semantic_remember(peek, "null") is False
        assert sc.semantic_recall_stats()["entries"] == 0, "空结果被钉进缓存 = 灌完库也读不到新东西"

    def test_concurrent_first_use_constructs_exactly_one_instance(self, monkeypatch):
        """双检锁的**内层**判据只在并发首用时显形：8 个线程同时进来，没有内层判据就各建一份
        （每份 16–24ms 起步，且互相覆盖 ``_EMBEDDINGS_INSTANCE``）。

        这条存在的理由是变异实测的结论：串行用例证不到内层判据（摘掉内层 ⇒ 外层快路径仍挡住
        重建 ⇒ 全绿），反过来摘掉外层也只是把"无锁快路径"变成"抢一次锁"，构造次数不变——
        那是性能面而非行为面，本批刻意不为它写守卫（写得出断言的只有锁竞争，不可靠）。
        """
        import time

        embeddings = _FakeEmbeddings({"q": _NEAR_A})
        built: list = []

        def _slow_factory(config=None):
            built.append(config)
            time.sleep(0.05)  # 放大竞争窗口：让后来者必然在锁外先通过了外层判据
            return embeddings

        import agentix.retrieval.infra.embedding.client as client_module

        monkeypatch.setattr(client_module, "create_embeddings", _slow_factory)
        threads = [
            threading.Thread(target=sc._embeddings_or_none, name=f"embed-{i}") for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert len(built) == 1, f"并发首用建了 {len(built)} 份 embeddings（双检锁内层判据失效）"

    def test_remember_is_a_noop_when_flag_turned_off_mid_flight(self, monkeypatch):
        """flag 是运行期读的（``_config()``）⇒ 关掉之后旧 peek 也不该继续写。"""
        monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", True)
        peek = SemanticPeek(bucket="b", query="q", vector=tuple(_NEAR_A), match=None)
        monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", False)
        assert semantic_remember(peek, _payload("x")) is False
        assert sc.semantic_recall_stats()["entries"] == 0


# ===========================================================================
# 6. peek + remember 端到端（进程内真存储）
# ===========================================================================


class TestPeekRememberRoundTrip:
    def test_reworded_question_hits_after_first_round_trip(self, monkeypatch):
        embeddings = _FakeEmbeddings({"民法典诉讼时效几年": _NEAR_A, "诉讼时效是多久": _NEAR_B})
        _install_embeddings(monkeypatch, embeddings)
        monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", True)
        payload = _payload("第188条")

        first = semantic_recall_peek("kb1|t1", "民法典诉讼时效几年")
        assert first is not None and first.match is None
        assert semantic_remember(first, payload) is True

        second = semantic_recall_peek("kb1|t1", "诉讼时效是多久")
        assert second is not None and second.hit
        assert second.match.payload == payload
        assert second.match.score == pytest.approx(0.96, abs=1e-12)

    def test_unrelated_question_does_not_hit_even_after_write(self, monkeypatch):
        embeddings = _FakeEmbeddings({"合同违约责任": _NEAR_A, "浏览器内核版本": _FAR}, model="m-x")
        _install_embeddings(monkeypatch, embeddings)
        monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", True)
        first = semantic_recall_peek("kb1|t1", "合同违约责任")
        assert semantic_remember(first, _payload("违约")) is True
        again = semantic_recall_peek("kb1|t1", "浏览器内核版本")
        assert again is not None and not again.hit

    def test_different_bucket_scope_cannot_see_each_other(self, monkeypatch):
        """权限隔离是结构性的：换个作用域就是换桶，向量再像也不许命中。"""
        embeddings = _FakeEmbeddings({"q1": _NEAR_A, "q2": _NEAR_B})
        _install_embeddings(monkeypatch, embeddings)
        monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", True)
        a = semantic_recall_peek("tenantA:kb1", "q1")
        assert semantic_remember(a, _payload("A 的文档")) is True
        b = semantic_recall_peek("tenantB:kb1", "q2")
        assert (
            b is not None and not b.hit
        ), "跨作用域命中 = 把 A 的文档答给 B（权限事故，不是缓存事故）"
        assert semantic_recall_stats()["entries"] == 1
        assert semantic_remember(b, _payload("B 的文档")) is True
        assert semantic_recall_stats()["buckets"] == 2, "两个作用域必须各占一桶，不共池"

    def test_bucket_carries_the_model_name_so_spaces_are_not_pooled(self, monkeypatch):
        """口径 4：换模型后新旧向量不在同一空间；共池的最坏形态是静默命中错答案。"""
        table = {"q1": _NEAR_A, "q2": _NEAR_B}
        first = _FakeEmbeddings(table, model="bge-m3")
        built = _install_embeddings(monkeypatch, first)
        monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", True)
        peek = semantic_recall_peek("scope", "q1")
        assert peek.bucket.endswith("|m:bge-m3")
        assert semantic_remember(peek, _payload("旧空间的文档")) is True

        import agentix.retrieval.config as cfg_module

        real_get_default = cfg_module.get_default_config

        def _other_model():
            out = dict(real_get_default())
            out["embedding_model"] = "another-model"
            return out

        monkeypatch.setattr(cfg_module, "get_default_config", _other_model)
        second = _FakeEmbeddings(table, model="another-model")
        monkeypatch.setattr(
            "agentix.retrieval.infra.embedding.client.create_embeddings",
            lambda config=None: (built.append(config), second)[1],
        )
        after = semantic_recall_peek("scope", "q2")
        assert after.bucket.endswith("|m:another-model")
        assert after is not None and not after.hit, "跨向量空间命中 = 用两把尺的数值互比"

    def test_write_back_reuses_the_peek_vector_not_a_second_embedding(self, monkeypatch):
        """回写重新嵌入 ⇒ "判定用的向量"与"存进去的向量"分两次取，换端点后就对不上号。"""
        embeddings = _FakeEmbeddings({"q": _NEAR_A})
        _install_embeddings(monkeypatch, embeddings)
        monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", True)
        peek = semantic_recall_peek("scope", "q")
        semantic_remember(peek, _payload("doc"))
        assert embeddings.queries == ["q"], f"多打了一次 embedding：{embeddings.queries}"


# ===========================================================================
# 7. 接线守卫（**必须用 AST，不许用子串**）
#
# 沿用 p127 §6 的教训：那三条初版是文本子串守卫，把 `if ...EMBEDDING_CACHE_PREWARM_ENABLED:`
# 改成 `if True:` 后 25 条**全绿**——键名还留在上一行注释里，`in` 与 `index()` 都由散文满足，
# 而注释不是控制流。本组因此一律按"调用是否位于那个 if 分支的 body 里"来判，
# 变异实测见 `.tmp/p128_mutation.py`。
# ===========================================================================


def _chat_tree() -> ast.Module:
    return ast.parse(CHAT_SERVICE_PY.read_text(encoding="utf-8"))


def _retrieve_fn() -> ast.FunctionDef:
    for node in ast.walk(_chat_tree()):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "retrieve_scoped_docs_cached":
            return node
    raise AssertionError("retrieve_scoped_docs_cached 不见了")


def _refs(node: ast.AST, name: str) -> bool:
    """引用了这个名字？Attribute（``m.name``）与裸 Name（导入后直接调用）都算。"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr == name:
            return True
        if isinstance(sub, ast.Name) and sub.id == name:
            return True
    return False


def _to_thread_calls(fn: ast.AST, name: str) -> list[ast.Call]:
    """``asyncio.to_thread(name, ...)`` 或 ``to_thread(name, ...)`` 形式的调用。"""
    out: list[ast.Call] = []
    for call in ast.walk(fn):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        is_to_thread = (isinstance(func, ast.Attribute) and func.attr == "to_thread") or (
            isinstance(func, ast.Name) and func.id == "to_thread"
        )
        if not is_to_thread or not call.args:
            continue
        if _refs(call.args[0], name):
            out.append(call)
    return out


def _flag_branch(fn: ast.AST, flag: str) -> ast.If:
    """找到 test 里引用了该 flag 的那个 ``if``（判据是 AST，不是行文本）。"""
    candidates = [
        node for node in ast.walk(fn) if isinstance(node, ast.If) and _refs(node.test, flag)
    ]
    assert candidates, f"没有任何 if 的**判据**引用了 {flag}（只在注释里提到不算）"
    return candidates[0]


class TestWiringStructure:
    def test_peek_call_sits_inside_the_flag_branch(self):
        fn = _retrieve_fn()
        branch = _flag_branch(fn, "SEMANTIC_CACHE_ENABLED")
        peek_calls = _to_thread_calls(branch, "semantic_recall_peek")
        assert peek_calls, "语义 peek 必须在 flag 分支内被调用"

    def test_no_peek_call_anywhere_outside_the_flag_branch(self):
        """反向检查：flag 分支之外一个 peek 都不许有——"等价"靠的是不执行，不是返回空。"""
        fn = _retrieve_fn()
        branch = _flag_branch(fn, "SEMANTIC_CACHE_ENABLED")
        inside = set(id(node) for node in ast.walk(branch))
        orphans = [
            node
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and id(node) not in inside
            and any(_refs(arg, "semantic_recall_peek") for arg in node.args)
        ]
        assert not orphans, f"flag 分支之外仍在调用 peek：{[n.lineno for n in orphans]}"

    def test_peek_runs_off_the_event_loop(self):
        """peek 会同步打 embedding（sqlite 内容缓存 + 同步 http）⇒ 按住事件循环等于拖慢全实例。"""
        fn = _retrieve_fn()
        branch = _flag_branch(fn, "SEMANTIC_CACHE_ENABLED")
        direct = [
            node
            for node in ast.walk(branch)
            if isinstance(node, ast.Call)
            and _refs(node.func, "semantic_recall_peek")
            and not (isinstance(node.func, ast.Attribute) and node.func.attr == "to_thread")
        ]
        assert not direct, "peek 必须经 asyncio.to_thread 下放，不许直接 await/同调"

    def test_semantic_lookup_happens_after_the_exact_key(self):
        """顺序是成本口径的一部分：精确键命中连 embedding 都不必算；反过来等于给每次
        命中白付一次 embedding —— 把省钱件改造成花钱件。

        判据只认 ``asyncio.to_thread(cache_redis.get, ...)`` 这一个形状：早先版本用
        "任何 .get 调用"来找，结果把 miss 分支里的 ``report.get`` 也算成精确键读取，
        测出"顺序倒置"的假红——守卫的判据必须与它声称守护的那一次调用同源。
        """
        fn = _retrieve_fn()
        branch = _flag_branch(fn, "SEMANTIC_CACHE_ENABLED")
        exact_gets = [
            node
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", "") == "to_thread"
            and node.args
            and isinstance(node.args[0], ast.Attribute)
            and node.args[0].attr == "get"
        ]
        assert exact_gets, "精确键读取（to_thread(cache_redis.get, …)）不见了"
        first_exact = min(node.lineno for node in exact_gets)
        assert (
            first_exact < branch.lineno
        ), f"精确键读取在 {first_exact} 行、语义层在 {branch.lineno} 行 ⇒ 顺序倒置"

    def test_write_back_is_guarded_by_the_peek_and_runs_off_the_loop(self):
        fn = _retrieve_fn()
        calls = _to_thread_calls(fn, "semantic_remember")
        assert calls, "回写缺失：miss 之后没人把结果存进语义层，这层缓存永远不命中"
        guards = [
            node
            for node in ast.walk(fn)
            if isinstance(node, ast.If) and _refs(node.test, "semantic_peek")
        ]
        assert guards, "回写没有以 peek 存在为前提（flag 关时 peek 恒为 None ⇒ 天然不写）"
        for call in calls:
            assert any(
                any(sub is call for sub in ast.walk(guard)) for guard in guards
            ), f"第 {call.lineno} 行的回写在任何 semantic_peek 判据之外"

    def test_scope_for_semantic_layer_comes_from_the_same_key_builder(self):
        """口径 3：bucket 必须是 ``_compose_key(scope_only=True)``——不许另拼一份隔离串。"""
        fn = _retrieve_fn()
        branch = _flag_branch(fn, "SEMANTIC_CACHE_ENABLED")
        composed = [
            node
            for node in ast.walk(branch)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_compose_key"
            and any(
                kw.arg == "scope_only" and isinstance(kw.value, ast.Constant) and kw.value.value
                for kw in node.keywords
            )
        ]
        assert composed, "语义 bucket 不是从精确键构造器派生的"
        assert _refs(fn, "_retrieval_cache_key"), "链路里已没有共享的键构造器"

    def test_scope_only_segment_drops_only_the_question_digest(self):
        """纯函数侧：``question_norm=None`` 只把摘要段换成字面量 ``sem``，其余段逐位相同。

        ``sem`` 与真实摘要（24 位十六进制）不可能相撞 ⇒ 精确键与语义作用域互不污染。
        """
        base = dict(kb_id="kb1", artifact_id="a1", search_k=5, scope_token="tok", tenant_id="t9")
        exact = cs._retrieval_cache_key(question_norm="合同违约责任", **base)
        scope = cs._retrieval_cache_key(question_norm=None, **base)
        digest = hashlib.sha256("合同违约责任".encode()).hexdigest()[:24]
        assert digest in exact and digest not in scope
        assert scope == exact.replace(f"{digest}|", "sem|")
        assert ":sem|" in scope
        for segment in ("t9:kb1:a1:5:tok",):
            assert segment in scope and segment in exact
        assert "sem" != digest and len(digest) == 24


# ===========================================================================
# 8. 真链路行为（retrieve_scoped_docs_cached）
# ===========================================================================


class _RecordingRedis:
    """只记录键的假 Redis。**载荷必须是 bytes**：真实 redis-py 返回 bytes，
    给 str 会让 ``_deserialize_docs_from_cache`` 在 ``.decode`` 上抛错、被外层
    ``except`` 吞成一次 miss——那会让"精确键命中短路语义层"这条用例假绿。
    """

    def __init__(self, payload=None):
        self.gets: list[str] = []
        self.sets: list[tuple[str, bytes]] = []
        self._payload = payload.encode("utf-8") if isinstance(payload, str) else payload

    def get(self, key):
        self.gets.append(key)
        return self._payload

    def set(self, key, value, **kwargs):
        self.sets.append((key, value))
        return True


async def _retrieve(*, question="合同违约责任", cache_redis=None, **kw):
    return await cs.retrieve_scoped_docs_cached(
        kb_id="kb1",
        question=question,
        search_k=kw.pop("search_k", 5),
        metadata_filter=None,
        vectorstore=object(),
        cache_redis=cache_redis,
        artifact_id="a1",
        scope_token="",
        session=None,
        **kw,
    )


class TestRetrievalChainBehaviour:
    @pytest.mark.asyncio
    async def test_flag_off_chain_is_bit_identical_and_touches_nothing_new(self, monkeypatch):
        """约定 1 的链路版：默认关 ⇒ 两个名字都不许被调用（桩一调用就 raise）。"""
        calls: list[int] = []

        def _dense(vectorstore, query, **kwargs):
            calls.append(1)
            return [Document(page_content="违约条款", metadata={"chunk_id": "c1"})]

        monkeypatch.setattr(cs, "retrieve_docs_with_retry", _dense)
        monkeypatch.setattr(sc, "semantic_recall_peek", _boom_peek)
        monkeypatch.setattr(sc, "semantic_remember", _boom_remember)
        import agentix.retrieval.infra.embedding.client as client_module

        monkeypatch.setattr(
            client_module,
            "create_embeddings",
            lambda config=None: (_ for _ in ()).throw(AssertionError("flag-off 构造了 embeddings")),
        )
        monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", False)

        docs = await _retrieve()
        assert [d.page_content for d in docs] == ["违约条款"]
        assert calls == [1]

    @pytest.mark.asyncio
    async def test_semantic_hit_skips_the_vector_store_entirely(self, monkeypatch):
        payload = _payload("语义命中的文档")

        def _fake_peek(bucket_scope, query):
            from agentix.retrieval.infra.rag.semantic_cache import SemanticMatch

            return SemanticPeek(
                bucket=bucket_scope,
                query=query,
                vector=tuple(_NEAR_A),
                match=SemanticMatch(payload=payload, score=0.97, age_seconds=1.0),
            )

        monkeypatch.setattr(sc, "semantic_recall_peek", _fake_peek)
        monkeypatch.setattr(sc, "semantic_remember", lambda *a: False)
        monkeypatch.setattr(cs, "retrieve_docs_with_retry", _forbidden_dense)
        monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", True)

        docs = await _retrieve()
        assert [d.page_content for d in docs] == ["语义命中的文档"]

    @pytest.mark.asyncio
    async def test_bucket_scope_is_the_exact_key_minus_the_question_digest(self, monkeypatch):
        """口径 3 的链路版：捕获真正传进语义层的 scope，与精确 Redis 键做差分。"""
        seen: dict[str, str] = {}

        def _fake_peek(bucket_scope, query):
            seen["bucket_scope"] = bucket_scope
            seen["query"] = query
            return None

        monkeypatch.setattr(sc, "semantic_recall_peek", _fake_peek)
        monkeypatch.setattr(sc, "semantic_remember", lambda *a: False)
        monkeypatch.setattr(cs, "retrieve_docs_with_retry", _quiet_dense)
        monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", True)
        redis = _RecordingRedis()

        await _retrieve(question="合同违约责任", cache_redis=redis)
        exact = redis.gets[0]
        digest = hashlib.sha256(cs._normalize_question("合同违约责任").encode()).hexdigest()[:24]
        assert seen["bucket_scope"] == exact.replace(f"{digest}|", "sem|")
        assert seen["query"] == cs._normalize_question("合同违约责任")
        # 隔离段必须逐个还在（租户/kb/artifact/search_k）——少一段就是跨权限命中
        assert "default:kb1:a1:5:" in seen["bucket_scope"]
        assert cs._fusion_fingerprint() in seen["bucket_scope"]

    @pytest.mark.asyncio
    async def test_miss_writes_back_the_fresh_result_with_the_peek_vector(self, monkeypatch):
        captured: list[tuple] = []
        peek = SemanticPeek(bucket="scope|m:fake", query="q", vector=tuple(_NEAR_A), match=None)
        monkeypatch.setattr(sc, "semantic_recall_peek", lambda *a: peek)
        monkeypatch.setattr(
            sc, "semantic_remember", lambda p, payload: captured.append((p, payload))
        )
        docs = [Document(page_content="新检索到的", metadata={"chunk_id": "c9"})]
        monkeypatch.setattr(cs, "retrieve_docs_with_retry", lambda *a, **k: docs)
        monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", True)

        out = await _retrieve()
        assert [d.page_content for d in out] == ["新检索到的"]
        assert captured and captured[0][0] is peek, "回写必须复用 peek 的向量，不重新嵌入"
        assert captured[0][1] == cs._serialize_docs_for_cache(docs)

    @pytest.mark.asyncio
    async def test_bad_payload_falls_back_to_a_real_search(self, monkeypatch):
        """坏载荷不是"检索失败"，是一次 miss（约定 2）。"""
        from agentix.retrieval.infra.rag.semantic_cache import SemanticMatch

        def _fake_peek(bucket_scope, query):
            return SemanticPeek(
                bucket=bucket_scope,
                query=query,
                vector=tuple(_NEAR_A),
                match=SemanticMatch(payload="{[ not json", score=0.99, age_seconds=0.5),
            )

        monkeypatch.setattr(sc, "semantic_recall_peek", _fake_peek)
        monkeypatch.setattr(sc, "semantic_remember", lambda *a: False)
        monkeypatch.setattr(
            cs,
            "retrieve_docs_with_retry",
            lambda *a, **k: [Document(page_content="兜底结果", metadata={"chunk_id": "c1"})],
        )
        monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", True)

        docs = await _retrieve()
        assert [d.page_content for d in docs] == ["兜底结果"]

    @pytest.mark.asyncio
    async def test_empty_payload_is_not_a_hit_and_gets_overwritten(self, monkeypatch):
        """空结果若算命中，"检索为空"会粘在整个 TTL 里——IndexWorker 灌完库也读不到新东西。"""
        from agentix.retrieval.infra.rag.semantic_cache import SemanticMatch

        def _fake_peek(bucket_scope, query):
            return SemanticPeek(
                bucket=bucket_scope,
                query=query,
                vector=tuple(_NEAR_A),
                match=SemanticMatch(payload="[]", score=0.99, age_seconds=0.5),
            )

        written: list[str] = []
        monkeypatch.setattr(sc, "semantic_recall_peek", _fake_peek)
        monkeypatch.setattr(sc, "semantic_remember", lambda p, payload: written.append(payload))
        monkeypatch.setattr(
            cs,
            "retrieve_docs_with_retry",
            lambda *a, **k: [Document(page_content="灌库后的新文档", metadata={"chunk_id": "n1"})],
        )
        monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", True)

        docs = await _retrieve()
        assert [d.page_content for d in docs] == ["灌库后的新文档"]
        assert written and "灌库后的新文档" in written[0]

    @pytest.mark.asyncio
    async def test_exhausted_search_does_not_poison_the_semantic_layer(self, monkeypatch):
        """检索返回空 ⇒ 什么都不写（口径：空结果不进缓存），也不许报错。"""
        peek = SemanticPeek(bucket="scope|m:fake", query="q", vector=tuple(_NEAR_A), match=None)
        written: list[str] = []
        monkeypatch.setattr(sc, "semantic_recall_peek", lambda *a: peek)
        monkeypatch.setattr(sc, "semantic_remember", lambda p, payload: written.append(payload))
        monkeypatch.setattr(cs, "retrieve_docs_with_retry", lambda *a, **k: [])
        monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", True)

        docs = await _retrieve()
        assert docs == []
        assert written == []

    @pytest.mark.asyncio
    async def test_embedding_outage_leaves_the_chain_answering_as_usual(self, monkeypatch):
        """断向量服务仍可答：peek 走真实实现，但 create_embeddings 一调用就 raise。"""

        def _dense(vectorstore, query, **kwargs):
            return [Document(page_content="照常回答", metadata={"chunk_id": "c1"})]

        monkeypatch.setattr(cs, "retrieve_docs_with_retry", _dense)
        import agentix.retrieval.infra.embedding.client as client_module

        monkeypatch.setattr(
            client_module,
            "create_embeddings",
            lambda config=None: (_ for _ in ()).throw(RuntimeError("LM Studio down")),
        )
        monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", True)

        docs = await _retrieve()
        assert [d.page_content for d in docs] == ["照常回答"]

    @pytest.mark.asyncio
    async def test_exact_redis_hit_short_circuits_the_semantic_layer(self, monkeypatch):
        """精确键命中连 embedding 都不必算——语义层只接"换了问法"那部分流量。"""
        redis = _RecordingRedis(payload=_payload("精确命中的文档"))
        monkeypatch.setattr(sc, "semantic_recall_peek", _boom_peek)
        monkeypatch.setattr(sc, "semantic_remember", _boom_remember)
        monkeypatch.setattr(cs, "retrieve_docs_with_retry", _forbidden_dense)
        monkeypatch.setattr(rs, "SEMANTIC_CACHE_ENABLED", True)

        docs = await _retrieve(cache_redis=redis)
        assert [d.page_content for d in docs] == ["精确命中的文档"]


def _boom_peek(bucket_scope, query):
    raise AssertionError("flag-off 路径不该调用 semantic_recall_peek")


def _boom_remember(*args):
    raise AssertionError("flag-off 路径不该调用 semantic_remember")


def _forbidden_dense(*args, **kwargs):
    raise AssertionError("语义命中时不该再打向量库")


def _quiet_dense(*args, **kwargs):
    return [Document(page_content="兜底", metadata={"chunk_id": "c0"})]


# ===========================================================================
# 9. 文档同步面（检索域键的四处：settings / 校验 / .env.example / 规划文档）
# ===========================================================================


class TestDocSync:
    def test_env_example_documents_the_three_keys_as_comments(self):
        block = (REPO / ".env.example").read_text(encoding="utf-8")
        for key in _SEMANTIC_KEYS:
            assert f"# {key}=" in block, f"{key} 没在 .env.example 里出现（且必须是注释示例）"
            assert (
                f"\n{key}=" not in block
            ), f"{key} 写成了生效行：会让缺项自检把每个新部署报成配置变更"
        assert "SEMANTIC_CACHE" in block

    def test_project_spec_registers_keys_and_validation(self):
        spec = (REPO / "docs" / "项目实现手册.md").read_text(encoding="utf-8")
        assert "SEMANTIC_CACHE_ENABLED=false" in spec
        assert "SEMANTIC_CACHE_TAU ∈ (0,1]" in spec

    def test_metric_label_allowlist_still_covers_what_we_emit(self):
        """约定 3 的复核：本批复用的 label 名早已在白名单里（白名单没被本批改动）。"""
        source = (REPO / "tests" / "test_p100_genai_observability.py").read_text(encoding="utf-8")
        for label in ("cache_type", "component"):
            assert f'"{label}"' in source
