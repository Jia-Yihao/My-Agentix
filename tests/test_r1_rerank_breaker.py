"""R1-3：Reranker 熔断测试。

依据：《技术变更与架构演进指南 v2.0》§2.1.5③——pybreaker 式熔断包住
外部依赖；开闸降级（跳过重排、只回本地原序）。
"""

from __future__ import annotations

import asyncio

import pytest

from agentix.reliability.circuit_breaker import CircuitState, registry as breaker_registry
from agentix.retrieval.infra.rag import rerank as rerank_mod


@pytest.fixture(autouse=True)
def _reset_breaker():
    breaker_registry._breakers.clear()  # 彻底清空（reset_all 保留旧实例参数，会跨用例污染）
    yield
    breaker_registry._breakers.clear()


class _FakeDoc:
    def __init__(self, text: str):
        self.page_content = text


def test_rerank_skipped_for_single_doc():
    docs = [_FakeDoc("a")]
    assert asyncio.run(rerank_mod.rerank_documents("q", docs)) == docs


def test_rerank_success_orders_by_relevance(monkeypatch):
    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.2},
                ]
            }

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _FakeResponse()

    monkeypatch.setattr(rerank_mod.httpx, "AsyncClient", _FakeClient)

    docs = [_FakeDoc("低分"), _FakeDoc("高分")]
    result = asyncio.run(rerank_mod.rerank_documents("q", docs))

    assert result[0].page_content == "高分"


def test_infinity_cohere_rerank_contract(monkeypatch):
    requests = []

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "object": "rerank",
                "model": "text-embedding-bge-reranker-v2-m3",
                "results": [
                    {"index": 1, "relevance_score": 0.91},
                    {"index": 0, "relevance_score": 0.12},
                ],
                "usage": {"prompt_tokens": 10, "total_tokens": 10},
            }

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, endpoint, **kwargs):
            requests.append((endpoint, kwargs["json"]))
            return _FakeResponse()

    monkeypatch.setattr(rerank_mod.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setenv("RERANKER_BASE_URL", "http://127.0.0.1:7997/")
    monkeypatch.setenv("RERANKER_MODEL", "text-embedding-bge-reranker-v2-m3")
    docs = [_FakeDoc("first candidate"), _FakeDoc("best candidate")]

    ranked, applied = asyncio.run(
        rerank_mod.rerank_documents_with_status("which is most relevant?", docs)
    )

    assert applied is True
    assert [doc.page_content for doc in ranked] == ["best candidate", "first candidate"]
    assert requests == [
        (
            "http://127.0.0.1:7997/rerank",
            {
                "model": "text-embedding-bge-reranker-v2-m3",
                "query": "which is most relevant?",
                "documents": ["first candidate", "best candidate"],
                "top_n": 2,
            },
        )
    ]


def test_http_200_error_payload_is_reported_as_degraded(monkeypatch, caplog):
    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"error": "Unexpected endpoint"}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _FakeResponse()

    monkeypatch.setattr(rerank_mod.httpx, "AsyncClient", _FakeClient)
    docs = [_FakeDoc("first"), _FakeDoc("second")]

    ranked, applied = asyncio.run(rerank_mod.rerank_documents_with_status("q", docs))

    assert ranked == docs
    assert applied is False
    assert "rerank service returned an error" in caplog.text


def test_breaker_opens_after_threshold_and_serves_original_order(monkeypatch):
    """连续失败达阈值 → OPEN；后续调用快速降级（原序返回，不再打上游）。"""
    calls = {"n": 0}

    def _fail_post(*a, **k):
        calls["n"] += 1
        raise rerank_mod.httpx.ConnectError("rerank down")

    class _FailingClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            _fail_post()

    monkeypatch.setattr(rerank_mod.httpx, "AsyncClient", _FailingClient)

    docs = [_FakeDoc("1"), _FakeDoc("2")]

    async def _drive():
        for _ in range(5):  # threshold=5（默认）
            result = await rerank_mod.rerank_documents("q", docs)
            assert result == docs  # 失败降级原序

        assert breaker_registry.get(rerank_mod._RERANK_BREAKER_NAME).state is CircuitState.OPEN
        upstream_calls_at_open = calls["n"]

        # OPEN 期间：快速降级，上游调用数不再增长
        for _ in range(3):
            result = await rerank_mod.rerank_documents("q", docs)
            assert result == docs
        assert calls["n"] == upstream_calls_at_open

    asyncio.run(_drive())


def test_breaker_recovers_after_success(monkeypatch):
    """半开探测成功 → CLOSED，重排恢复生效。"""
    import time as _time

    class _OkClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            class _Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"results": [{"index": 1, "relevance_score": 0.9}]}

            return _Resp()

    # 直接构造 OPEN 状态（threshold=1）
    breaker = breaker_registry.get_or_create(
        rerank_mod._RERANK_BREAKER_NAME, failure_threshold=1, recovery_timeout=0.05
    )
    breaker._failure_count = 1
    breaker._transition_to(CircuitState.OPEN)

    async def _drive():
        # 等待恢复窗口 → HALF_OPEN
        await asyncio.sleep(0.06)
        state = breaker.state
        # 断言失败时必须能自证现场。
        #
        # 背景（2026-09-23）：本用例在全量跑里**偶发失败过一次**，而当时只留下
        # "FAILED ..." 一行，**没有任何能判断成因的信息**——这类"有结论没证据"的
        # 记录本身就是下一个坑（教训见说明书 §8.7）。这里把两个**能把机制区分开**
        # 的量写进断言消息：
        #
        #   recovery_timeout != 0.05 ⇒ `get_or_create` 返回了**别人的**同名实例
        #     （即"首建者胜"拿到了别处用 5/60 建的那个）。该路径现在会打一条
        #     "already exists ... ignoring this call's params" 的 warning，
        #     所以 **日志里有没有这条 warning，本身就是判据**。
        #   recovery_timeout == 0.05 ⇒ 配置没问题，而是 `_last_state_change`
        #     在那 60ms 窗口里被**别的并发调用者**刷新了（OPEN 计时被重置）。
        #     —— 这是本仓"共享全局熔断器单例"的固有风险，不是状态机缺陷。
        assert state is CircuitState.HALF_OPEN, (
            f"恢复窗口后仍未转 HALF_OPEN: state={state} "
            f"recovery_timeout={breaker.recovery_timeout} "
            f"failure_threshold={breaker.failure_threshold} "
            f"last_state_change={breaker._last_state_change} "
            f"now={_time.monotonic()}"
        )

    asyncio.run(_drive())

    # 探测成功 ×2（success_threshold 默认 2）→ CLOSED
    monkeypatch.setattr(rerank_mod.httpx, "AsyncClient", _OkClient)
    docs = [_FakeDoc("低"), _FakeDoc("高")]

    async def _probe():
        for _ in range(2):
            result = await rerank_mod.rerank_documents("q", docs)
            assert result[0].page_content == "高"
        assert breaker_registry.get(rerank_mod._RERANK_BREAKER_NAME).state is CircuitState.CLOSED

    asyncio.run(_probe())
