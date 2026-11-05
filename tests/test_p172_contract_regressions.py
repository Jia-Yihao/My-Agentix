"""p172——T13「回填前四类契约回归」：检索逐位 / 意图输出 / 状态机 / SSE 帧序列。

本文件是四类契约回归的行为基线。
本文件是**行为基线**，不是新能力的验收：任何回填/优化（T17 的 A/B 取证等）
动工之前，先把四类契约的现状钉死，让"改动改变了什么"在测试里可读、可归因。

四类各自钉什么（每类一组可红可绿的契约用例；红由注入变异证明，见任务单）：

1. ``TestRetrievalBitExact``——给定**固定语料 + 固定 embedding 打分桩**，
   检索管线（``retrieve_docs_with_retry`` 密集路 / ``retrieve_hybrid_docs``
   融合路）产出的 **chunk_id 序列、顺序与分数**逐位钉死。用例只调原接口、
   只断言可观测输出，**不复制**被测的过滤/排序/融合逻辑——复制等于用被测
   对象自己当基准（T13 风险行：契约回归若复制被测逻辑即违反基准方法学）。
2. ``TestIntentOutput``——``analyze_and_route_query`` 的意图输出。输入表覆盖
   寒暄/致谢、单条数据条目直取、长句（>60 字符）、空串、普通问句；分
   ``ROUTING_FASTPATH_ENABLED`` 关/开两组，钉死 **reason 词汇**
   （fast-path 命中 ``chit_chat`` / ``statute_direct``；兜底 ``exception`` /
   ``invalid_type``）+ **目标意图名**（``Router.type``）+ 生产侧 ``logic`` 措辞。
   ``route_type → 节点名`` 的映射只在第 3 类钉（同一个判定不许两个源）。
   长度闸（``len(text) > 60``）的**灵敏度**由两条专门构造的样本承担：
   ``_LONG_GREETING``（超长寒暄）与 ``_LONG_STATUTE_QUESTION``（超长单条数据
   条目）——它们的构造前提（无长度闸时分别命中 ``chit_chat`` / ``statute_direct``）
   由 ``test_long_inputs_really_exceed_the_length_gate`` 静态守卫。
3. ``TestStateMachine``——``build_graph`` 编译后的**边集**（谁接谁）、
   ``route_query`` 的分派结果、以及终态（每个落点都直接到 ``__end__``）。
   既是「给定 route_type 的节点跳转序列」，也是「少一条边」的棘轮。
4. ``TestSseFrameSequence``——脚本化一次运行的 SSE **帧序列**（``type`` 名与
   顺序 + 路由帧/终帧关键字段）。帧一律由生产投影层
   ``ChatService.stream_events`` 产出：本文件只负责"回放 LangGraph 事件 +
   读帧"，**不另造帧解析器**（沿用 ``tests/test_p65_unified_sse.py`` 的范式）。

收口批另钉第五类指标契约（``TestLlmCallsSavedReasonBound``）：
``record_llm_calls_saved`` 的 ``reason`` 按**形状**限界——``[A-Za-z0-9_]{1,32}``
逐字通过、空串仍落 ``unknown``、越界形态落 ``other``；上面的"四类"编号保持 T13
原始范围不动。

全部用例离线可跑：无真实 LLM、无 Redis/Postgres/Milvus（全部走桩）。
复验：``.venv/Scripts/python.exe -m pytest tests/test_p172_contract_regressions.py -q``

已知近似（未钉住的面）
----------------------

本文件钉的是**能钉住的**那些面。下面这些是刻意没钉的，不要把它们读成"已验证"：

1. **浮点容差**：融合分断言用 ``pytest.approx(abs=1e-15)`` 而非逐位相等——
   RRF 的 ``Σ 1/(k+rank)`` 是 IEEE754 双精度求和，末位跨平台可能不同。
2. **融合方法只覆盖 rrf**：``zscore`` 口径（``RETRIEVAL_FUSION_ALPHA`` 权重、
   mean/std 归一）在本文件零覆盖。
3. **SSE 只钉帧名与顺序**：帧里的耗时/时间戳类字段不做断言——那是墙钟读数，
   钉死等于往 CI 里埋一条随机炸弹。
4. **``reason="empty"`` 兜底不可达**：``Router.type`` 是 ``Literal[...]``（不含空串），
   直接构造与 ``router_cls.model_validate`` 两条路径都在校验阶段拒掉空串 ⇒
   ``_record_router_fallback("empty")`` 那一支在本文件构造不出来，未钉。
5. **边集快照带编译前提**：``_baseline_compiled_graph`` 在 ``NODE_RETRY_ENABLED`` /
   ``NODE_ERROR_HANDLER_ENABLED`` 关、``NODE_TIMEOUT_SECONDS=0`` 下编译。这三个
   策略 flag 打开会插包装节点、边集随之变化，那种拓扑不在覆盖范围内。
6. **缓存层 fail-open 未覆盖**：本文件直接调 ``retrieve_docs_with_retry`` /
   ``retrieve_hybrid_docs``，没走 ``retrieve_scoped_docs_cached`` ⇒ Redis 不可用时
   "静默回源 + warning"的降级噪声不在断言视野内。
7. **检索侧两个旋钮只做推演、不单独钉**：``EFFECTIVENESS_FILTER_ENABLED`` /
   ``RETRIEVAL_FETCH_K_ESCALATE`` 对本文件的 id/分数断言无影响（推演写在
   ``pinned_dense_settings`` 的 docstring 里），但"把旋钮翻过来再跑一遍"没有用例。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.documents import Document
from langchain_core.messages import HumanMessage

from agentix.application.agents import router as router_mod
from agentix.application.agents.router import analyze_and_route_query, route_query
from agentix.config import ablation
from agentix.config import settings as app_settings
from agentix.observability import metrics as metrics_mod
from agentix.orchestration.langgraph import lg_builder
from agentix.orchestration.langgraph.lg_states import (
    AgentState,
    CodeRouter,
    Router,
    TaskRouter,
)
from agentix.retrieval.usecases.chat_service import retrieve_docs_with_retry
from agentix.retrieval.usecases.retrieval_hybrid import retrieve_hybrid_docs


# ===========================================================================
# 1) 检索逐位
# ===========================================================================

#: 固定语料：(chunk_id, 向量分数)。真实后端（faiss/milvus/qdrant）的
#: ``similarity_search_with_score`` 返回 ``(doc, score)`` **且已按分数排好序**，
#: 因此桩按"已排序的固定打分"回放；顺序本身就是契约的一部分（管线不得重排）。
_DENSE_CORPUS = (
    ("doc-a", 0.10),
    ("doc-d", 0.35),
    ("doc-b", 0.55),
    ("doc-e", 1.10),
    ("doc-c", 1.60),
)


def _doc(chunk_id: str, *, text: str | None = None) -> Document:
    return Document(
        page_content=text if text is not None else f"content:{chunk_id}",
        metadata={"chunk_id": chunk_id, "document_id": f"parent:{chunk_id}"},
    )


class _FixedEmbeddingIndex:
    """固定语料 + 固定 embedding 打分桩（不算 embedding、不连向量库）。

    只实现 ``similarity_search_with_score``——即被测管线唯一真正打基础设施的
    那一步。回放的是**固定的**（文档，分数）序列，所以后续阈值过滤 / 有效性
    过滤 / 截断 / 融合的每一步都可逐位对照。
    """

    def __init__(self, pairs):
        self._pairs = list(pairs)
        self.calls: list[dict] = []
        self.index = SimpleNamespace(ntotal=len(self._pairs))

    def similarity_search_with_score(self, question, k=None, filter=None, fetch_k=None):
        self.calls.append({"question": question, "k": k, "filter": filter, "fetch_k": fetch_k})
        limit = fetch_k or k or len(self._pairs)
        return list(self._pairs[:limit])


@pytest.fixture
def pinned_dense_settings(monkeypatch):
    """把影响"逐位"读数的检索旋钮钉成基线值。

    宿主 ``.env`` 可以把 ``RETRIEVAL_MAX_DISTANCE`` / ``INDEX_SCORE_IS_SIMILARITY``
    改成别的口径；不钉住的话这条基线会随机器漂移，"逐位"就变成了"逐机器"。

    另外两个默认开的旋钮**刻意不钉**，因为它们不影响本文件的 id/分数断言。
    推演（读的是 ``retrieve_docs_with_retry`` 的实际控制流）：

    - ``EFFECTIVENESS_FILTER_ENABLED``（默认 ``true``）会在阈值过滤之前先走
      ``filter_in_force``。本文件语料的 metadata 只有 ``chunk_id`` /
      ``document_id``，**缺全部有效性字段** ⇒ ``is_in_force`` 按"缺失字段视为
      现行有效"返回 True ⇒ 一条都不被剔除，id 序列与分数原样保留。
    - 同一开关让 ``post_filtered=True``，于是 ``_resolve_retrieval_fetch_k`` 走
      有界分支，结果是 ``max(base, min(ntotal, bounded))``。语料
      ``ntotal=5`` 小而 ``base=max(search_k*4, 20)=20`` ⇒ ``fetch_k`` 恒等于
      base（20），"多取的候选"根本不存在，后端返回的仍是同一批 5 条。
    - ``RETRIEVAL_FETCH_K_ESCALATE``（默认 ``true``）只在"召回不足且
      ``fetch_k < ntotal``"时升级；此处 ``fetch_k=20 < ntotal=5`` 恒假 ⇒
      不会二次检索，每个用例恰好一次后端调用（口径另见
      ``test_dense_search_call_caliber``）。
    """
    from agentix.retrieval.settings import settings as retrieval_settings

    monkeypatch.setattr(retrieval_settings, "INDEX_SCORE_IS_SIMILARITY", False)
    monkeypatch.setattr(retrieval_settings, "RETRIEVAL_MAX_DISTANCE", 1.2)
    monkeypatch.setattr(retrieval_settings, "RETRIEVAL_MIN_SCORE", 0.0)
    return retrieval_settings


def _ids_and_scores(docs) -> list[tuple[str, float]]:
    return [(d.metadata["chunk_id"], d.metadata["_retrieval_score"]) for d in docs]


class TestRetrievalBitExact:
    """检索管线产出逐位：id 序列 / 顺序 / 分数，全部走原接口取可观测输出。"""

    def test_dense_distance_caliber_filters_and_keeps_backend_order(self, pinned_dense_settings):
        """距离口径（默认）：阈值外的被滤掉，**保序**、不重排、分数原样落 metadata。"""
        index = _FixedEmbeddingIndex([(_doc(cid), score) for cid, score in _DENSE_CORPUS])

        docs = retrieve_docs_with_retry(
            index, "民法典 第一百四十三条", search_k=4, metadata_filter=None
        )

        assert _ids_and_scores(docs) == [
            ("doc-a", 0.10),
            ("doc-d", 0.35),
            ("doc-b", 0.55),
            ("doc-e", 1.10),
        ]
        # doc-c(1.60) 越过 RETRIEVAL_MAX_DISTANCE=1.2 ⇒ 被滤掉而不是排到末位
        assert "doc-c" not in [d.metadata["chunk_id"] for d in docs]

    def test_dense_search_call_caliber(self, pinned_dense_settings):
        """传参口径：``k`` 就是 ``search_k``，``fetch_k`` 是放大后的候选量（=20）。

        为什么这条也属"逐位"：``k`` / ``fetch_k`` 是管线交给后端的**唯一**两个
        数量控制；它们一旦漂移，上面三条的绿可能来自"多取了候选"而不是管线本身
        正确。语料 ``ntotal=5`` 小于 ``base=20`` ⇒ ``fetch_k`` 钉成 20；同时
        反向钉住"恰好一次调用"（升级条件 ``20 < 5`` 恒假，见
        ``pinned_dense_settings`` 的推演）。
        """
        index = _FixedEmbeddingIndex([(_doc(cid), score) for cid, score in _DENSE_CORPUS])

        retrieve_docs_with_retry(index, "民法典 第一百四十三条", search_k=4, metadata_filter=None)

        assert index.calls == [
            {"question": "民法典 第一百四十三条", "k": 4, "filter": None, "fetch_k": 20}
        ]

    def test_dense_truncates_to_search_k(self, pinned_dense_settings):
        """``search_k`` 是截断位：截断发生在过滤之后（前两名即语料前两名）。"""
        index = _FixedEmbeddingIndex([(_doc(cid), score) for cid, score in _DENSE_CORPUS])

        docs = retrieve_docs_with_retry(
            index, "民法典 第一百四十三条", search_k=2, metadata_filter=None
        )

        assert _ids_and_scores(docs) == [("doc-a", 0.10), ("doc-d", 0.35)]

    def test_similarity_caliber_filters_without_resorting(self, pinned_dense_settings, monkeypatch):
        """相似度口径：``>= RETRIEVAL_MIN_SCORE`` 保留，且**仍不重排**。

        "不重排"是这里的要害：后端返回什么顺序，管线就吐什么顺序——一旦有人
        在过滤后补一次 sort，本用例与上一条会同时变红。
        """
        monkeypatch.setattr(pinned_dense_settings, "INDEX_SCORE_IS_SIMILARITY", True)
        monkeypatch.setattr(pinned_dense_settings, "RETRIEVAL_MIN_SCORE", 0.5)
        index = _FixedEmbeddingIndex([(_doc(cid), score) for cid, score in _DENSE_CORPUS])

        docs = retrieve_docs_with_retry(index, "民法典 第一百四十三条", search_k=5)

        assert _ids_and_scores(docs) == [
            ("doc-b", 0.55),
            ("doc-e", 1.10),
            ("doc-c", 1.60),
        ]

    @pytest.mark.asyncio
    async def test_hybrid_rrf_fusion_order_and_scores(self, pinned_dense_settings, monkeypatch):
        """混合路（dense + sparse → RRF）融合后的 id 序列与融合分逐位钉死。

        融合分是 RRF 的 ``Σ 1/(k+rank)``（k 取 ``RETRIEVAL_RRF_K`` 默认 60），
        期望值在这里写成**字面量小数**而不是重新算一遍公式——重算就是把被测
        逻辑抄进用例，公式一变两边一起变，断言永远绿。
        """
        retrieval_settings = pinned_dense_settings
        monkeypatch.setattr(retrieval_settings, "RETRIEVAL_SPARSE_ENABLED", True)
        monkeypatch.setattr(retrieval_settings, "RETRIEVAL_HYBRID_RERANK", False)
        monkeypatch.setattr(retrieval_settings, "RETRIEVAL_FUSION_METHOD", "rrf")
        monkeypatch.setattr(retrieval_settings, "RETRIEVAL_RRF_K", 60)
        monkeypatch.setattr(retrieval_settings, "RETRIEVAL_FUSION_TOP_N", 200)

        index = _FixedEmbeddingIndex(
            [
                (_doc("d1", text="dense:d1"), 0.20),
                (_doc("d2", text="dense:d2"), 0.30),
                (_doc("d3", text="dense:d3"), 0.40),
            ]
        )
        sparse_pairs = [
            (_doc("d2", text="sparse:d2"), 12.0),
            (_doc("d4", text="sparse:d4"), 9.0),
        ]

        def _fake_sparse_search(**kwargs):
            return list(sparse_pairs)

        monkeypatch.setattr("agentix.retrieval.infra.rag.sparse.sparse_search", _fake_sparse_search)

        docs, report = await retrieve_hybrid_docs(
            kb_id="kb-p172",
            question="民法典 第一百四十三条",
            question_norm="民法典 第一百四十三条",
            search_k=4,
            metadata_filter=None,
            vectorstore=index,
            artifact_id="artifact-p172",
            rewrite_ms=0.0,
            session=object(),
        )

        assert [d.metadata["chunk_id"] for d in docs] == ["d2", "d1", "d4", "d3"]
        assert [d.metadata["_retrieval_score"] for d in docs] == pytest.approx(
            [0.03252247488101534, 0.016393442622950821, 0.016129032258064516, 0.015873015873015872],
            rel=0,
            abs=1e-15,
        )
        # 跨通道同 chunk_id：dense 文档优先（索引 metadata 更全）
        assert docs[0].page_content == "dense:d2"
        assert report["fusion_method"] == "rrf"
        assert report["docs_count"] == 4

    @pytest.mark.asyncio
    async def test_hybrid_dense_only_keeps_dense_order_and_scores(
        self, pinned_dense_settings, monkeypatch
    ):
        """sparse 关时单通道：保 dense 原序与原分，不写融合分。"""
        retrieval_settings = pinned_dense_settings
        monkeypatch.setattr(retrieval_settings, "RETRIEVAL_SPARSE_ENABLED", False)
        monkeypatch.setattr(retrieval_settings, "RETRIEVAL_HYBRID_RERANK", False)

        index = _FixedEmbeddingIndex([(_doc("d1"), 0.20), (_doc("d2"), 0.30), (_doc("d3"), 0.40)])

        docs, report = await retrieve_hybrid_docs(
            kb_id="kb-p172",
            question="民法典 第一百四十三条",
            question_norm="民法典 第一百四十三条",
            search_k=3,
            metadata_filter=None,
            vectorstore=index,
            artifact_id="artifact-p172",
            rewrite_ms=0.0,
            session=None,
        )

        assert _ids_and_scores(docs) == [("d1", 0.20), ("d2", 0.30), ("d3", 0.40)]
        assert report["sparse"] == "off"


# ===========================================================================
# 2) 意图输出
# ===========================================================================

#: 长句（>60 字符）对照组：本条**对长度闸不敏感**——它含「如何」「适用」「和」
#: 等 ``_REASONING_MARKERS``，也不是寒暄整句，因此即便删掉长度闸也走不到
#: ``statute_direct``、更不会被 ``_CHATTER_EXACT`` 整句命中。它钉的是"长句照常
#: 走 LLM"，长度闸的**灵敏度**由下面两条承担（删长度闸时必须变红）。
_LONG_QUESTION = (
    "民法典第一百四十三条规定的民事法律行为有效条件在实际审判中究竟应当如何理解和适用"
    "请结合最高人民法院的审判意见和相关司法解释作进一步分析说明"
)

#: 超长寒暄（62 字符）：「！」落在 ``_CHATTER_EXACT`` 尾部字符类
#: ``[!！。.~～,，、\s]*$`` 内 ⇒ 若删掉长度闸，本条会被整句命中、走 ``chit_chat``。
#: 长度闸的灵敏度就靠这一点。
_LONG_GREETING = "你好" + "！" * 60

#: 超长单条数据条目（67 字符）：``_STATUTE_REF`` 能搜到"第264条"，且整条**不含**
#: 任一 ``_REASONING_MARKERS`` ⇒ 若删掉长度闸，本条会走 ``statute_direct``。
#: 用「，」补长度是刻意的：``_fastpath_router`` 先 ``strip()``，尾部空格会被削掉。
_LONG_STATUTE_QUESTION = "刑法第264条" + "，" * 60


class _StubStructuredModel:
    """取代 ``get_llm_client`` 的角色模型：``with_structured_output`` 回放固定输出。

    只在**角色模型的出口**上打桩（``get_llm_client`` 是路由节点唯一的外部模型
    入口），被测节点本体一行不改。
    """

    def __init__(self, output=None, *, error: Exception | None = None):
        self._output = output
        self._error = error
        self.schemas: list = []
        self.calls = 0

    def with_structured_output(self, cls):
        self.schemas.append(cls)
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._output


def _boom(**kwargs):
    raise AssertionError("fast-path 应短路：不得构造/调用 LLM 分类器")


@pytest.fixture
def reason_sink(monkeypatch):
    """观察路由侧"reason 词汇"的两条唯一出口（fast-path 命中 / 兜底）。

    - ``record_llm_calls_saved(reason)``：fast-path 命中时记住的 reason；
    - ``record_router_fallback(reason)``：兜底出口的 reason（节点内懒加载导入，
      所以钉在 metrics 模块上）。
    """
    saved: list[str] = []
    fallback: list[str] = []
    monkeypatch.setattr(router_mod, "record_llm_calls_saved", saved.append)
    monkeypatch.setattr(metrics_mod, "record_router_fallback", fallback.append)
    return SimpleNamespace(saved=saved, fallback=fallback)


def _state(question: str) -> AgentState:
    return AgentState(messages=[HumanMessage(content=question)])


#: flag-off 组：桩输出**刻意与 fast-path 的结论相反**（寒暄/条文直取都给
#: ``kb-query``），因此这组同时钉住「flag 关 = 不短路」。
_LLM_GROUP = (
    ("你好", Router(type="kb-query", logic="llm: greeting -> kb"), "kb-query"),
    ("谢谢！", Router(type="kb-query", logic="llm: thanks -> kb"), "kb-query"),
    ("刑法第264条", Router(type="kb-query", logic="llm: statute -> kb"), "kb-query"),
    ("今天天气怎么样", Router(type="general-query", logic="llm: general"), "general-query"),
    ("", Router(type="kb-query", logic="llm: empty"), "kb-query"),
    (_LONG_QUESTION, Router(type="graphrag-query", logic="llm: long"), "graphrag-query"),
)

#: fast-path 命中表：``(问题, 期望 reason, 期望意图名, 期望 logic)``。
_FASTPATH_HIT_GROUP = (
    (
        "你好",
        "chit_chat",
        "general-query",
        "fastpath: chit-chat (deterministic, LLM classifier skipped)",
    ),
    (
        "谢谢！",
        "chit_chat",
        "general-query",
        "fastpath: chit-chat (deterministic, LLM classifier skipped)",
    ),
    (
        "刑法第264条",
        "statute_direct",
        "graphrag-query",
        "fastpath: single statute direct (deterministic, LLM classifier skipped)",
    ),
)

#: fast-path 未命中表：任一都不许短路（含长句、空串、寒暄夹带实质内容、多条文）。
#: 其中 ``_LONG_GREETING`` / ``_LONG_STATUTE_QUESTION`` 是**唯一拦住它们的理由
#: 就是长度闸**的两条——删掉长度闸它们必然变红（变异证明见提交材料）。
_FASTPATH_MISS_QUESTIONS = (
    _LONG_QUESTION,
    _LONG_GREETING,
    _LONG_STATUTE_QUESTION,
    "",
    "今天天气怎么样",
    "你好，帮我看看这个合同的风险点",
    "刑法第264条和第265条",
)


@pytest.fixture(autouse=True)
def _pinned_ablation_flags(monkeypatch):
    """把消融开关钉回生产现状默认值：``router_enabled`` 必须为 ``True``。

    做法与理由：``analyze_and_route_query`` 第一件事就是
    ``ablation.is_enabled("router_enabled")``，为假则**整体绕过**意图分类、
    固定回 ``kb-query``。宿主若导出了
    ``AGENTIX_ABLATION_ROUTER_ENABLED=false``，本文件第 2 类用例会以
    "模型没被调用 / reason 没记"的形式假红，与契约无关。

    键名不手写：取 ``ablation.ENV_PREFIX`` + 模块自己在 ``FLAG_DEFAULTS`` 里
    认的开关名（大写）；设置后立刻用模块自己的 ``is_enabled`` 读回自证。
    还原交给 ``monkeypatch.setenv``（本文件不直接改 ``os.environ``）。
    """
    monkeypatch.setenv(f"{ablation.ENV_PREFIX}ROUTER_ENABLED", "true")
    assert ablation.is_enabled("router_enabled") is True


class TestIntentOutput:
    """路由节点的意图输出：reason 词汇 + 目标意图名（节点映射见第 3 类）。"""

    def test_long_inputs_really_exceed_the_length_gate(self):
        """夹具自证：三条长输入确实 >60 字符，且两条灵敏度样本**只因**长度闸被挡。

        后三条断言是长度闸灵敏度的静态守卫：若 ``_CHATTER_EXACT`` 的尾部字符类
        收窄到不认「！」、或 ``_LONG_STATUTE_QUESTION`` 里混进了推理词，变异用例
        就会静默变绿（钉的是别的东西），这里先拦下来。
        """
        from agentix.application.agents.router import (
            _CHATTER_EXACT,
            _REASONING_MARKERS,
            _STATUTE_REF,
        )

        assert len(_LONG_QUESTION) > 60
        assert len(_LONG_GREETING) == 62
        assert len(_LONG_STATUTE_QUESTION) == 67
        # 无长度闸时会被整句命中 ⇒ 走 chit_chat
        assert _CHATTER_EXACT.fullmatch(_LONG_GREETING) is not None
        # 无长度闸时会成为 statute_direct（须同时满足"搜到条文"且"无推理词"）
        assert _STATUTE_REF.search(_LONG_STATUTE_QUESTION) is not None
        assert [m for m in _REASONING_MARKERS if m in _LONG_STATUTE_QUESTION] == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("question, stub_output, expected_type", _LLM_GROUP)
    async def test_flag_off_goes_through_llm_classifier(
        self, monkeypatch, reason_sink, question, stub_output, expected_type
    ):
        """flag 关：逐条走 LLM 分类，输出=模型给的类型+措辞，且不记"省下的调用"。"""
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        model = _StubStructuredModel(stub_output)
        monkeypatch.setattr(router_mod, "get_llm_client", lambda **kwargs: model)

        result = await analyze_and_route_query(_state(question), config={})

        assert result["router"].type == expected_type
        assert result["router"].logic == stub_output.logic
        assert result["router"].question == question
        assert model.calls == 1
        assert reason_sink.saved == [], "flag 关不得记 llm_calls_saved"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "question, expected_reason, expected_type, expected_logic", _FASTPATH_HIT_GROUP
    )
    async def test_flag_on_hit_pins_reason_vocabulary_and_intent(
        self, monkeypatch, reason_sink, question, expected_reason, expected_type, expected_logic
    ):
        """flag 开 + 命中：reason 词汇、意图名、logic 措辞三者同时钉死，且零 LLM 调用。"""
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", True)
        monkeypatch.setattr(router_mod, "get_llm_client", _boom)

        result = await analyze_and_route_query(_state(question), config={})

        assert reason_sink.saved == [expected_reason]
        assert result["router"].type == expected_type
        assert result["router"].logic == expected_logic
        assert result["router"].question == question.strip()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("question", _FASTPATH_MISS_QUESTIONS)
    async def test_flag_on_miss_falls_through_to_llm(self, monkeypatch, reason_sink, question):
        """flag 开 + 未命中：不记 reason、照常调模型（模型给什么就是什么）。"""
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", True)
        model = _StubStructuredModel(Router(type="kb-query", logic="llm: no fastpath"))
        monkeypatch.setattr(router_mod, "get_llm_client", lambda **kwargs: model)

        result = await analyze_and_route_query(_state(question), config={})

        assert reason_sink.saved == []
        assert model.calls == 1
        assert (result["router"].type, result["router"].logic) == (
            "kb-query",
            "llm: no fastpath",
        )

    @pytest.mark.asyncio
    async def test_flag_off_keeps_baseline_schema(self, monkeypatch, reason_sink):
        """flag 关时结构化输出 schema 仍是基类 ``Router``（逐字节等价的前提）。"""
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        monkeypatch.setattr(app_settings, "TASK_EXEC_ENABLED", False)
        monkeypatch.setattr(app_settings, "CODE_EXEC_ENABLED", False)
        model = _StubStructuredModel(Router(type="kb-query", logic="llm"))
        monkeypatch.setattr(router_mod, "get_llm_client", lambda **kwargs: model)

        await analyze_and_route_query(_state("今天天气怎么样"), config={})

        assert model.schemas == [Router]

    @pytest.mark.asyncio
    async def test_llm_exception_reports_reason_exception(self, monkeypatch, reason_sink):
        """LLM 抛错：reason 词汇 = ``exception``，兜底回 kb-query 并保留固定措辞。"""
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        monkeypatch.setattr(
            router_mod,
            "get_llm_client",
            lambda **kwargs: _StubStructuredModel(error=RuntimeError("llm down")),
        )

        result = await analyze_and_route_query(_state("今天天气怎么样"), config={})

        assert reason_sink.fallback == ["exception"]
        assert result["router"].type == "kb-query"
        assert result["router"].logic == "fallback: default to knowledge base routing"

    @pytest.mark.asyncio
    async def test_invalid_type_reports_reason_invalid_type(self, monkeypatch, reason_sink):
        """模型给出白名单外的类型（task-query 且 flag 关）：reason = ``invalid_type``。"""
        monkeypatch.setattr(app_settings, "ROUTING_FASTPATH_ENABLED", False)
        monkeypatch.setattr(app_settings, "TASK_EXEC_ENABLED", False)
        monkeypatch.setattr(
            router_mod,
            "get_llm_client",
            lambda **kwargs: _StubStructuredModel(
                TaskRouter(type="task-query", logic="llm: wants task execution")
            ),
        )

        result = await analyze_and_route_query(_state("今天天气怎么样"), config={})

        assert reason_sink.fallback == ["invalid_type"]
        assert result["router"].type == "kb-query"
        # 兜底沿用模型给的 logic（有则用），无则用固定措辞
        assert result["router"].logic == "llm: wants task execution"


# ===========================================================================
# 3) 状态机
# ===========================================================================

_START = "__start__"
_END = "__end__"

#: 主图边集基线（编译后 ``get_graph()`` 的 (source, target)，含条件边）。
#: 这是"谁接谁"的唯一真源快照：多一条/少一条都是状态机契约变更。
_EXPECTED_EDGES = {
    (_START, "message_window"),
    ("message_window", "analyze_and_route_query"),
    ("analyze_and_route_query", "respond_to_general_query"),
    ("analyze_and_route_query", "get_additional_info"),
    ("analyze_and_route_query", "create_research_plan"),
    ("analyze_and_route_query", "create_image_query"),
    ("analyze_and_route_query", "create_file_query"),
    ("analyze_and_route_query", "create_kb_query"),
    ("analyze_and_route_query", "execute_task"),
    ("analyze_and_route_query", "code_agent"),
    ("respond_to_general_query", _END),
    ("get_additional_info", _END),
    ("create_research_plan", _END),
    ("create_image_query", _END),
    ("create_file_query", _END),
    ("create_kb_query", _END),
    ("execute_task", _END),
    ("code_agent", _END),
}

#: 意图名 → 落点节点（flag 无关的 7 项基线路由）。
_ROUTE_DISPATCH = (
    ("general-query", "respond_to_general_query"),
    ("additional-query", "get_additional_info"),
    ("graphrag-query", "create_research_plan"),
    ("text2sql-query", "create_research_plan"),
    ("image-query", "create_image_query"),
    ("file-query", "create_file_query"),
    ("kb-query", "create_kb_query"),
)

_COMPILED: dict[str, object] = {}


def _baseline_compiled_graph():
    """按基线开关编译一次主图；策略类 flag 钉住关，边集只反映拓扑本身。"""
    if "graph" not in _COMPILED:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(app_settings, "NODE_RETRY_ENABLED", False)
            mp.setattr(app_settings, "NODE_TIMEOUT_SECONDS", 0)
            mp.setattr(app_settings, "NODE_ERROR_HANDLER_ENABLED", False)
            _COMPILED["graph"] = lg_builder.build_graph(checkpointer=None)
    return _COMPILED["graph"]


def _compiled_edges(compiled) -> set[tuple[str, str]]:
    return {(e.source, e.target) for e in compiled.get_graph().edges}


class TestStateMachine:
    """节点跳转序列与终态：边集棘轮 + 分派表 + 逐意图的完整链路。"""

    def test_edge_set_is_bit_exact(self):
        """主图边集与基线逐条相等（多边=擅自接线，少边=状态机丢跳转）。"""
        edges = _compiled_edges(_baseline_compiled_graph())

        assert edges == _EXPECTED_EDGES, (
            f"主图边集漂移：少={sorted(_EXPECTED_EDGES - edges)} "
            f"多={sorted(edges - _EXPECTED_EDGES)}"
        )

    @pytest.mark.parametrize("route_type, expected_node", _ROUTE_DISPATCH)
    def test_route_type_jump_sequence(self, route_type, expected_node):
        """给定 route_type：分派结果 == 期望节点，且该链路每一跳都在边集里。"""
        edges = _compiled_edges(_baseline_compiled_graph())
        state = AgentState(messages=[HumanMessage(content="q")], router=Router(type=route_type))

        dispatched = route_query(state)

        assert dispatched == expected_node
        sequence = (_START, "message_window", "analyze_and_route_query", dispatched, _END)
        assert all(
            hop in edges for hop in zip(sequence, sequence[1:])
        ), f"{route_type} 的跳转链路断了：{[h for h in zip(sequence, sequence[1:]) if h not in edges]}"

    @pytest.mark.parametrize(
        "flag, router_obj, on_node",
        [
            ("TASK_EXEC_ENABLED", TaskRouter(type="task-query", logic="t"), "execute_task"),
            ("CODE_EXEC_ENABLED", CodeRouter(type="code-query", logic="c"), "code_agent"),
        ],
    )
    def test_flag_gated_nodes_reachability(self, monkeypatch, flag, router_obj, on_node):
        """flag 开→进专属节点；flag 关→防御性落回 create_kb_query（终态不变）。"""
        edges = _compiled_edges(_baseline_compiled_graph())
        state = AgentState(messages=[HumanMessage(content="q")], router=router_obj)

        monkeypatch.setattr(app_settings, flag, True)
        assert route_query(state) == on_node
        assert ("analyze_and_route_query", on_node) in edges
        assert (on_node, _END) in edges

        monkeypatch.setattr(app_settings, flag, False)
        assert route_query(state) == "create_kb_query"

    def test_every_landing_node_terminates(self):
        """终态钉死：每个落点都直接到 ``__end__``，不存在悬挂节点。"""
        edges = _compiled_edges(_baseline_compiled_graph())
        landings = {target for source, target in edges if source == "analyze_and_route_query"}

        assert landings == {
            "respond_to_general_query",
            "get_additional_info",
            "create_research_plan",
            "create_image_query",
            "create_file_query",
            "create_kb_query",
            "execute_task",
            "code_agent",
        }
        assert {(node, _END) for node in landings} <= edges


# ===========================================================================
# 4) SSE 帧序列
# ===========================================================================


class _FakeChunk:
    def __init__(self, content):
        self.content = content


class _ReplayGraph:
    """按顺序回放脚本化 LangGraph 事件；``exc`` 非空时回放完再抛。"""

    def __init__(self, events, *, exc: BaseException | None = None):
        self._events = list(events)
        self._exc = exc

    def astream_events(self, input_state, *, config=None, version=None, **kwargs):
        """回放事件流。

        ``**kwargs`` 是**刻意的**：生产 ``ChatService._stream_events_impl`` 还会
        传 ``durability=`` 与 ``control=``（``GRAPH_DURABILITY`` 非默认 /
        ``RUN_CONTROL_ENABLED=true`` 时）。把签名钉成三个形参，flag 一开这里就
        退化成 ``TypeError``——挂掉的是夹具而不是契约（假红）。真图会消费这些
        形参，本桩与它一样"接受但忽略"。这样做比在 fixture 里把两个开关全部钉死
        更合适：钉死只能保证"当前默认下不炸"，签名放宽则连 flag 打开也不再假红。
        """
        events, exc = self._events, self._exc

        async def _gen():
            for ev in events:
                yield ev
            if exc is not None:
                raise exc

        return _gen()


def _ev(kind, name=None, data=None, metadata=None):
    return {"event": kind, "name": name, "data": data or {}, "metadata": metadata or {}}


def _route_event() -> dict:
    return _ev(
        "on_chain_end",
        name="analyze_and_route_query",
        data={
            "output": {
                "router": Router(
                    type="kb-query",
                    logic="fallback: default to knowledge base routing",
                    question="问题",
                )
            }
        },
        metadata={"langgraph_node": "analyze_and_route_query"},
    )


def _graph_end_event(output: dict) -> dict:
    return _ev("on_chain_end", name="LangGraph", data={"output": output}, metadata={})


_SOURCES = [{"document_id": "d1", "source": "d1"}]


async def _collect(events, *, exc=None, session_id="s-p172"):
    """经生产投影层收帧（``stream_events`` = 线上 SSE 那条链，不在测试里另写解析）。"""
    from agentix.application.services.chat_service import ChatService

    graph = _ReplayGraph(events, exc=exc)
    service = ChatService(graph_getter=lambda: graph)
    return [frame async for frame in service.stream_events("问题", session_id)]


@pytest.fixture(autouse=True)
def _isolated_runtime():
    """每例独立的 ControlPlane / 轨迹记录器（防运行记录与落盘跨用例串扰）。"""
    from agentix.application.services.trajectory import reset_trajectory_recorder
    from agentix.runtime.control_plane import reset_control_plane

    reset_control_plane()
    reset_trajectory_recorder()
    yield
    reset_control_plane()
    reset_trajectory_recorder()


class TestSseFrameSequence:
    """一次脚本化运行的帧序列：``type`` 名与顺序 + 关键字段。"""

    @pytest.mark.asyncio
    async def test_success_run_frame_sequence(self):
        """正常流：processing → route → tool_start → 2×message → tool_end → done。"""
        events = [
            _route_event(),
            _ev("on_tool_start", name="milvus_search"),
            _ev(
                "on_chat_model_stream",
                name="ChatModel",
                data={"chunk": _FakeChunk("答")},
                metadata={"langgraph_node": "create_kb_query"},
            ),
            _ev(
                "on_chat_model_stream",
                name="ChatModel",
                data={"chunk": _FakeChunk("案")},
                metadata={"langgraph_node": "create_kb_query"},
            ),
            _ev("on_tool_end", name="milvus_search"),
            _graph_end_event({"sources": _SOURCES}),
        ]

        frames = await _collect(events)

        assert [frame["type"] for frame in frames] == [
            "metadata",
            "metadata",
            "tool_start",
            "message",
            "message",
            "tool_end",
            "done",
        ]
        route_frames = [frame for frame in frames if frame.get("route")]
        assert len(route_frames) == 1
        assert route_frames[0]["route"] == "kb-query"
        assert route_frames[0]["metadata"] == {
            "route": "kb-query",
            "logic": "fallback: default to knowledge base routing",
        }
        assert [f["content"] for f in frames if f["type"] == "message"] == ["答", "案"]
        assert frames[-1]["metadata"]["sources"] == _SOURCES
        assert [frame["seq"] for frame in frames] == list(range(1, len(frames) + 1))

    @pytest.mark.asyncio
    async def test_no_token_run_falls_back_to_whole_text_before_done(self):
        """一个 token 都没转发：整段兜底的 message 必须出现在 done **之前**且只一次。"""
        events = [
            _route_event(),
            _graph_end_event({"messages": [{"content": "整段答案"}], "sources": _SOURCES}),
        ]

        frames = await _collect(events)

        assert [frame["type"] for frame in frames] == [
            "metadata",
            "metadata",
            "message",
            "done",
        ]
        assert frames[2]["content"] == "整段答案"

    @pytest.mark.asyncio
    async def test_failed_run_frame_sequence(self):
        """失败流：路由帧之后抛异常 ⇒ error 帧 + 兜底 done（不伪造成功内容）。"""
        frames = await _collect(
            [_route_event()], exc=RuntimeError("stream boom"), session_id="s-p172-err"
        )

        assert [frame["type"] for frame in frames] == [
            "metadata",
            "metadata",
            "error",
            "done",
        ]
        assert frames[-1]["metadata"]["sources"] == []


# ===========================================================================
# 5) LLM_CALLS_SAVED reason 限界
# ===========================================================================


class TestLlmCallsSavedReasonBound:
    """``record_llm_calls_saved`` 的 reason 形状限界：词表内逐字透传、空串落
    ``unknown``、越界形态落 ``other``（基数有界，约定 3）。"""

    @staticmethod
    def _count(reason: str) -> float:
        from prometheus_client import REGISTRY

        return REGISTRY.get_sample_value("llm_calls_saved_total", {"reason": reason}) or 0.0

    @classmethod
    def _delta(cls, recorded: str, expect: str) -> None:
        before = cls._count(expect)
        metrics_mod.record_llm_calls_saved(recorded)
        assert cls._count(expect) == before + 1, (
            f"reason {recorded!r} 应落 label {expect!r}"
        )

    @pytest.mark.parametrize(
        "reason",
        ["chit_chat", "statute_direct", "capability_meta", "identity_meta", "ack_meta"],
    )
    def test_fastpath_reasons_pass_through_verbatim(self, reason):
        """五个 fast-path reason 逐字透传（现树行为不许被限界截伤）。"""
        self._delta(reason, reason)

    def test_empty_reason_still_maps_to_unknown(self):
        """``reason or "unknown"`` 的既有空串行为必须保住（只紧不松）。"""
        self._delta("", "unknown")

    @pytest.mark.parametrize("reason", ["含中文", "a" * 40, "x y"])
    def test_out_of_shape_reasons_map_to_other(self, reason):
        """越界形态（中文/超长/带空格）落 ``other``，不进高基数 label。"""
        self._delta(reason, "other")
        # G21（U-A）：非 str 实参是最极端的越界形态（原形态在 ``re.match`` 抛 TypeError 上抛
        # 主链路）——守卫后只留痕跳过、不外抛，且不进任何 label（下面逐字透传用例的计数断言
        # 用的是 delta，本行不产生任何增量，故不会与相邻用例的计数相互干扰）。
        metrics_mod.record_llm_calls_saved(7)  # type: ignore[arg-type]

    def test_true_source_vocabulary_survives_the_bound(self):
        """reason 真源 ``router.FASTPATH_REASONS`` 的每个值经限界后恒等——把"生产侧
        形状限界不会截伤真源"钉住。测试里 import router 是允许的（测试文件不受分层
        允许集约束），生产侧 ``metrics.py`` 不能引它（test_p177:241 允许集）。"""
        for reason in router_mod.FASTPATH_REASONS:
            self._delta(reason, reason)
