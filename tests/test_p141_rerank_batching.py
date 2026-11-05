"""T9：rerank 候选分批 + 检索侧计数器。

验收①–③：

覆盖：
- 验收①（约定 1）：``RETRIEVAL_RERANK_BATCH_SIZE`` 默认 0 = 不分批 ⇒
  上游恰好一次调用、请求形状（endpoint / payload 键 / documents / top_n）
  与基线一致、结果与基线一致（用桩记录调用证明"单次"）。
- 验收③：配 batch_size ⇒ 调用次数 = ceil(候选数/批)，且融合顺序与
  不分批一致（显式口径：全局 relevance_score 降序；同分按原始下标升序）。
- 验收②（约定 3）：``rerank_calls_total`` / ``rerank_errors_total`` /
  ``rerank_latency_seconds`` 可查，且三个指标零 label（基数恒 1，天然
  无需进 p100 ALLOWED_METRIC_LABELS——该守卫枚举默认 REGISTRY 全部
  collector 的 label 名，无 label 即不受影响）。
- 约定 2（fail-open）：分批中途上游异常 ⇒ 主链不炸、保原序、
  errors 计数增加。

口径边界（刻意不测）：rerank 独立超时 + fail-open + 熔断为既有行为
（agentix/retrieval/settings.py:284、rerank.py:64-67,125-131），
本卡不重做，其回归由 tests/test_r1_rerank_breaker.py 继续钉住。
"""

from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace

import pytest

from agentix.infrastructure.http.shared_client import reset_shared_async_client
from agentix.reliability.circuit_breaker import registry as breaker_registry
from agentix.retrieval.infra.rag import rerank as rerank_mod

RERANK_KEYS = ("model", "query", "documents", "top_n")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # 熔断单例与共享 HTTP client 都是进程级状态：用例间必须清零，
    # 否则前一用例把 breaker 打到 OPEN，后一用例的上游调用根本发不出去。
    breaker_registry._breakers.clear()
    reset_shared_async_client()
    monkeypatch.setenv("RERANKER_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setenv("RERANKER_MODEL", "fake-rerank-model")
    yield
    breaker_registry._breakers.clear()
    reset_shared_async_client()


class _FakeDoc:
    def __init__(self, text: str):
        self.page_content = text


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def _install_fake_server(monkeypatch, scores: dict[str, float], fail_after: int | None = None):
    """替换 httpx.AsyncClient 为假 rerank 服务（沿用 test_r1_rerank_breaker 桩范式）。

    按 relevance_score 降序返回 results（与真实 reranker 行为一致），并把每次
    请求的 (endpoint, payload) 记进 state.payloads 供"单次/分批"断言。
    fail_after=k ⇒ 第 k+1 次调用抛 ConnectError（模拟分批中途上游异常）。
    """
    state = SimpleNamespace(calls=0, payloads=[], fail_after=fail_after, scores=scores)

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, endpoint, **kwargs):
            state.calls += 1
            payload = kwargs["json"]
            state.payloads.append((endpoint, payload))
            if state.fail_after is not None and state.calls > state.fail_after:
                raise rerank_mod.httpx.ConnectError("rerank down")
            results = [
                {"index": i, "relevance_score": state.scores[d]}
                for i, d in enumerate(payload["documents"])
            ]
            results.sort(key=lambda r: -r["relevance_score"])
            return _FakeResponse({"results": results})

    monkeypatch.setattr(rerank_mod.httpx, "AsyncClient", _FakeClient)
    return state


def _sample(name: str) -> float:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name) or 0.0


# ---------------------------------------------------------------------------
# 验收①：默认（batch_size=0）= 单次调用，与基线一致
# ---------------------------------------------------------------------------


def test_default_batch_size_is_zero():
    """约定 1：新键出厂默认必须是"不分批"（0）。"""
    assert rerank_mod.settings.RETRIEVAL_RERANK_BATCH_SIZE == 0


def test_default_off_single_call_and_baseline_request_shape(monkeypatch):
    """默认关 ⇒ 恰好一次上游调用，请求形状（endpoint + payload 键/值）与基线一致。"""
    scores = {"d0": 0.5, "d1": 0.9, "d2": 0.1, "d3": 0.7, "d4": 0.3}
    docs = [_FakeDoc(k) for k in scores]
    state = _install_fake_server(monkeypatch, scores)
    monkeypatch.setattr(rerank_mod.settings, "RETRIEVAL_RERANK_BATCH_SIZE", 0)

    ranked, applied = asyncio.run(rerank_mod.rerank_documents_with_status("q", docs))

    assert applied is True
    assert state.calls == 1
    endpoint, payload = state.payloads[0]
    assert endpoint == "http://127.0.0.1:8000/v1/rerank"
    # 基线 payload 形状：键集合与全候选 documents/top_n 逐位一致（不混入分批字段）
    assert tuple(payload) == RERANK_KEYS
    assert payload["model"] == "fake-rerank-model"
    assert payload["query"] == "q"
    assert payload["documents"] == ["d0", "d1", "d2", "d3", "d4"]
    assert payload["top_n"] == 5
    assert [d.page_content for d in ranked] == ["d1", "d3", "d0", "d4", "d2"]


# ---------------------------------------------------------------------------
# 分批：调用次数与融合顺序
# ---------------------------------------------------------------------------


def test_batched_call_count_and_fusion_matches_unbatched(monkeypatch):
    """batch_size=2、5 候选 ⇒ ceil(5/2)=3 次调用；融合顺序与不分批完全一致。"""
    scores = {"d0": 0.5, "d1": 0.9, "d2": 0.1, "d3": 0.7, "d4": 0.3}
    docs = [_FakeDoc(k) for k in scores]
    expected = ["d1", "d3", "d0", "d4", "d2"]

    # 参照系：不分批的结果顺序
    state_off = _install_fake_server(monkeypatch, scores)
    monkeypatch.setattr(rerank_mod.settings, "RETRIEVAL_RERANK_BATCH_SIZE", 0)
    ranked_off, _ = asyncio.run(rerank_mod.rerank_documents_with_status("q", docs))
    assert [d.page_content for d in ranked_off] == expected

    # 分批： breaker 是进程级单例，上一段失败/成功计数会串到这里，必须清掉重来
    breaker_registry._breakers.clear()
    state_on = _install_fake_server(monkeypatch, scores)
    monkeypatch.setattr(rerank_mod.settings, "RETRIEVAL_RERANK_BATCH_SIZE", 2)
    ranked_on, applied = asyncio.run(rerank_mod.rerank_documents_with_status("q", docs))

    assert applied is True
    assert state_on.calls == math.ceil(len(docs) / 2) == 3
    # 融合口径（验收③显式声明）：全局 relevance_score 降序 ⇒ 与不分批一致
    assert [d.page_content for d in ranked_on] == expected
    # 每批请求形状：documents 是连续切片、长度不超批大小、top_n 随批
    seen_docs: list[str] = []
    for _endpoint, payload in state_on.payloads:
        assert tuple(payload) == RERANK_KEYS
        assert len(payload["documents"]) <= 2
        assert payload["top_n"] == len(payload["documents"])
        seen_docs.extend(payload["documents"])
    # 候选不重不漏、按原始顺序切批
    assert seen_docs == ["d0", "d1", "d2", "d3", "d4"]


def test_batched_ties_fuse_by_original_index(monkeypatch):
    """同分跨批 ⇒ 按候选原始下标升序（显式口径：确定性、批次边界不影响同分次序）。"""
    scores = {"a": 0.9, "b": 0.5, "c": 0.9, "d": 0.1}
    docs = [_FakeDoc(k) for k in ("a", "b", "c", "d")]
    _install_fake_server(monkeypatch, scores)
    monkeypatch.setattr(rerank_mod.settings, "RETRIEVAL_RERANK_BATCH_SIZE", 2)

    ranked, applied = asyncio.run(rerank_mod.rerank_documents_with_status("q", docs))

    assert applied is True
    # a/c 同为 0.9：原始下标 a(0) 先于 c(2)
    assert [d.page_content for d in ranked] == ["a", "c", "b", "d"]


# ---------------------------------------------------------------------------
# 验收②：三个计数器可查、label 合规（零 label）
# ---------------------------------------------------------------------------


def test_rerank_metrics_registered_without_labels():
    """三个指标注册在默认 REGISTRY；零 label ⇒ 基数有界，无需进 p100 白名单。"""
    assert rerank_mod.RERANK_CALLS_TOTAL._labelnames == ()
    assert rerank_mod.RERANK_ERRORS_TOTAL._labelnames == ()
    assert rerank_mod.RERANK_LATENCY_SECONDS._labelnames == ()
    # 可查性：默认 REGISTRY 能枚举到样本名
    assert _sample("rerank_calls_total") >= 0.0
    assert _sample("rerank_errors_total") >= 0.0
    assert _sample("rerank_latency_seconds_count") >= 0.0


def test_counters_track_calls_and_latency(monkeypatch):
    """成功单次(1 调用) + 成功分批(4 候选 batch=3 ⇒ 2 调用)：
    calls +3、errors +0、latency 样本 +3（逐上游调用记账，一次检索多条计数）。"""
    scores = {"d0": 0.5, "d1": 0.9, "d2": 0.1, "d3": 0.7}
    docs = [_FakeDoc(k) for k in scores]
    c0, e0, l0 = (
        _sample("rerank_calls_total"),
        _sample("rerank_errors_total"),
        _sample("rerank_latency_seconds_count"),
    )
    state = _install_fake_server(monkeypatch, scores)

    monkeypatch.setattr(rerank_mod.settings, "RETRIEVAL_RERANK_BATCH_SIZE", 0)
    _, applied_off = asyncio.run(rerank_mod.rerank_documents_with_status("q", docs))
    # breaker 是进程级单例：上一段的成败计数会串到下一段，中间必须清掉
    breaker_registry._breakers.clear()
    monkeypatch.setattr(rerank_mod.settings, "RETRIEVAL_RERANK_BATCH_SIZE", 3)
    _, applied_on = asyncio.run(rerank_mod.rerank_documents_with_status("q", docs))

    assert applied_off is True and applied_on is True
    assert state.calls == 1 + math.ceil(4 / 3) == 3
    assert _sample("rerank_calls_total") - c0 == 3.0
    assert _sample("rerank_errors_total") - e0 == 0.0
    assert _sample("rerank_latency_seconds_count") - l0 == 3.0
    assert _sample("rerank_latency_seconds_sum") >= 0.0


# ---------------------------------------------------------------------------
# 约定 2：分批中途上游异常 ⇒ fail-open，主链不炸，errors 增加
# ---------------------------------------------------------------------------


def test_batch_failure_midway_fails_open_and_counts_errors(monkeypatch, caplog):
    """4 候选 batch=2，第二批次上游炸 ⇒ 保原序返回 applied=False，calls+2 / errors+1。"""
    scores = {"d0": 0.5, "d1": 0.9, "d2": 0.1, "d3": 0.7}
    docs = [_FakeDoc(k) for k in scores]
    c0, e0 = _sample("rerank_calls_total"), _sample("rerank_errors_total")
    _install_fake_server(monkeypatch, scores, fail_after=1)
    monkeypatch.setattr(rerank_mod.settings, "RETRIEVAL_RERANK_BATCH_SIZE", 2)

    ranked, applied = asyncio.run(rerank_mod.rerank_documents_with_status("q", docs))

    # 主链不炸：不抛异常、按原召回顺序 fail-open
    assert applied is False
    assert [d.page_content for d in ranked] == ["d0", "d1", "d2", "d3"]
    # 两次上游调用都记账（第二次失败）：calls +2、errors +1
    assert _sample("rerank_calls_total") - c0 == 2.0
    assert _sample("rerank_errors_total") - e0 == 1.0
    # fail-open 必须留痕（约定 2：logger.warning）
    assert "rerank degraded (fail-open)" in caplog.text


# ---------------------------------------------------------------------------
# 记账口径：分批路径行值解析失败与单次同形（ok=False）
# ---------------------------------------------------------------------------


def test_batched_row_parse_failure_counts_as_error(monkeypatch):
    """分批路径 rows 含非数值 index ⇒ 与单次同形记账：calls +1 / errors +1，
    异常仍原样冒泡到外层 fail-open 保原序（reviewer 第 1 轮一般项 3）。

    单次路径里同类 ValueError 发生在 ``_call_rerank`` 内部，被同一 except 计成
    ok=False；分批路径的解析必须落在同一记账范围内，否则同一件事两条路径
    errors_total 口径相反。
    """
    docs = [_FakeDoc(k) for k in ("d0", "d1", "d2", "d3")]
    c0, e0 = _sample("rerank_calls_total"), _sample("rerank_errors_total")

    class _BadRowResponse:
        def raise_for_status(self):
            pass

        def json(self):
            # 行是 dict 且含 index（通过形状校验），但 index 非数值 ⇒ int() 抛 ValueError
            return {"results": [{"index": "not-an-int", "relevance_score": 0.9}]}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, endpoint, **kwargs):
            return _BadRowResponse()

    monkeypatch.setattr(rerank_mod.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(rerank_mod.settings, "RETRIEVAL_RERANK_BATCH_SIZE", 2)

    ranked, applied = asyncio.run(rerank_mod.rerank_documents_with_status("q", docs))

    # 行为面不变：fail-open 保原序、applied=False
    assert applied is False
    assert [d.page_content for d in ranked] == ["d0", "d1", "d2", "d3"]
    # 记账面：首批即解析失败 ⇒ 恰一次上游调用，计一次失败
    assert _sample("rerank_calls_total") - c0 == 1.0
    assert _sample("rerank_errors_total") - e0 == 1.0
