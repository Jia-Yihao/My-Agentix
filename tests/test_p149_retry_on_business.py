"""LG-1-2 验收：``is_retryable`` 经 ``retry_on`` 成为「可重试」的唯一真源。

## 验收口径（指导文档 §4.2 LG-1-2「怎么算过」）

1. **参数化用例逐条同值**——同一批异常/状态码，新旧判定一致：业务分类
   ``is_retryable`` 与接线后的 ``readonly_retry_policy().retry_on`` 逐条对拍，
   并给出每条的期望布尔值；两条通路任何一条漂移都会红。
2. **断言实际生效的是自定义 ``retry_on``**——编译主图（flag-on）后读
   ``compiled.nodes[name].retry_policy[0].retry_on``，必须**就是**
   ``is_retryable`` 本身，不是框架默认 ``default_retry_on``。
3. **行为证明**：``httpx.HTTPStatusError(429)`` 在原生默认下**不重试**、在业务策略下
   重试一次成功——分类确实驱动了框架行为，而不是参数摆在那里。

## 与原生默认的四处刻意差异（都是业务口径赢）

| 异常 | 原生 ``default_retry_on`` | 业务 ``is_retryable`` | 理由 |
|---|---|---|---|
| ``httpx.HTTPStatusError(429)`` | `False`（只认 5xx） | **`True`** | 限流是瞬时故障 |
| ``TimeoutError`` | `False`（它是 `OSError` 子类，落在原生「不重试」清单） | **`True`** | 超时是最典型的瞬时故障 |
| ``openai.AuthenticationError``（401） | `True`（未知即重试） | **`False`** | 重试换不来鉴权，只会放大故障 |
| 未知异常（自定义 ``Exception`` 子类） | `True` | **`False`** | 重试换不来缺陷修复 |

判据不依赖网络：全部用本地构造的异常与内存图。
"""

from __future__ import annotations

import httpx
import openai
import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy, default_retry_on

from agentix.config import settings
from agentix.domain.policies.retry import is_retryable
from agentix.orchestration.langgraph import lg_builder
from agentix.orchestration.langgraph.policies import readonly_retry_policy

#: LG-1-1 接线的 6 个只读节点（真源：指导文档 §4.2 LG-1-1「做什么」行）。
READONLY_RETRY_NODES = (
    "analyze_and_route_query",
    "respond_to_general_query",
    "create_research_plan",
    "create_kb_query",
    "create_image_query",
    "create_file_query",
)


class _UnknownError(Exception):
    """既不在可重试特征里、也不在不可重试特征里的异常。"""


def _openai_rate_limit() -> Exception:
    response = httpx.Response(
        429,
        request=httpx.Request("POST", "http://127.0.0.1:1/v1/chat/completions"),
        text="Too Many Requests",
    )
    return openai.RateLimitError("Error code 429: rate limit exceeded", response=response, body=None)


def _openai_auth() -> Exception:
    response = httpx.Response(
        401, request=httpx.Request("POST", "http://127.0.0.1:1/v1/chat/completions")
    )
    return openai.AuthenticationError("Error code 401: invalid api key", response=response, body=None)


def _openai_bad_request() -> Exception:
    response = httpx.Response(
        400, request=httpx.Request("POST", "http://127.0.0.1:1/v1/chat/completions")
    )
    return openai.BadRequestError("Error code 400: bad request", response=response, body=None)


def _httpx_status(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://127.0.0.1:1/v1/chat/completions")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError(f"{code} status", request=request, response=response)


#: （id, 工厂, 业务口径期望值）。期望值写死在这里——``is_retryable`` 的行为变更
#: 必须同时改这一列，不允许让判据自己给自己当基准。
CASES = [
    ("openai.429", _openai_rate_limit, True),
    ("httpx.429", lambda: _httpx_status(429), True),
    ("openai.401", _openai_auth, False),
    ("httpx.401", lambda: _httpx_status(401), False),
    ("openai.400", _openai_bad_request, False),
    ("TimeoutError", lambda: TimeoutError("request timed out"), True),
    ("ConnectionResetError", lambda: ConnectionResetError("connection reset by peer"), True),
    ("ValueError", lambda: ValueError("bad value"), False),
    ("未知异常", lambda: _UnknownError("boom"), False),
]


def _retry_on_of(compiled, node_name: str):
    """读编译后节点上生效的重试策略回调（lg 把它存成元组，未传则为 None）。"""
    node = compiled.nodes[node_name]
    policies = node.retry_policy or ()
    assert len(policies) == 1, f"{node_name} 的 retry_policy 不是单策略：{policies!r}"
    return policies[0].retry_on


class TestWiredCallbackIsTheBusinessClassifier:
    @pytest.mark.parametrize(("exc_factory", "expected"), [(f, e) for _, f, e in CASES], ids=[i for i, _, _ in CASES])
    def test_wired_retry_on_matches_business_classification_item_by_item(self, exc_factory, expected):
        exc = exc_factory()
        assert is_retryable(exc) is expected, (
            f"业务分类对 {type(exc).__name__} 判 {is_retryable(exc)}，期望 {expected}"
            "——口径真变了，请同步本用例的期望列与 CHANGELOG"
        )
        assert readonly_retry_policy().retry_on(exc) is expected, (
            f"接线后的 retry_on 对 {type(exc).__name__} 判"
            f" {readonly_retry_policy().retry_on(exc)}，与业务分类不一致"
        )

    def test_retry_on_is_the_domain_function_itself_not_a_copy(self):
        assert readonly_retry_policy().retry_on is is_retryable, (
            "retry_on 必须直接是领域层 is_retryable（同一份实现），"
            "否则「唯一真源」只是说法"
        )

    def test_overrides_cannot_silently_drop_business_retry_on(self):
        """覆盖其它字段时 retry_on 保持不变；显式传 retry_on 才允许替换。"""
        assert readonly_retry_policy(max_attempts=2).retry_on is is_retryable
        assert readonly_retry_policy(retry_on=default_retry_on).retry_on is default_retry_on


class TestEffectivePolicyOnTheRealGraph:
    @pytest.fixture
    def compiled_on(self):
        original = settings.NODE_RETRY_ENABLED
        settings.NODE_RETRY_ENABLED = True
        try:
            yield lg_builder.build_graph(None)
        finally:
            settings.NODE_RETRY_ENABLED = original

    def test_every_wired_node_uses_business_retry_on(self, compiled_on):
        for name in READONLY_RETRY_NODES:
            assert name in compiled_on.nodes, f"编译图里没有节点 {name}"
            retry_on = _retry_on_of(compiled_on, name)
            assert retry_on is is_retryable, f"{name} 的 retry_on 不是业务分类：{retry_on!r}"
            assert retry_on is not default_retry_on, f"{name} 仍在用框架默认 retry_on"


class TestBusinessClassificationDrivesRealBehavior:
    """分类必须**改变框架行为**，而不只是参数对象里多一项。"""

    async def _run(self, policy, exc_factory) -> int:
        calls = []

        async def node(state: dict) -> dict:
            calls.append(1)
            raise exc_factory()

        builder = StateGraph(dict)
        builder.add_node("n", node, retry_policy=policy)
        builder.add_edge(START, "n")
        builder.add_edge("n", END)
        with pytest.raises(Exception) as excinfo:
            await builder.compile().ainvoke({"v": 1})
        assert isinstance(excinfo.value, type(exc_factory())), (
            f"抛出的不是原始异常：{type(excinfo.value).__name__}"
        )
        return len(calls)

    @pytest.mark.asyncio
    async def test_httpx_429_is_retried_by_business_policy_but_not_by_framework_default(self):
        assert await self._run(RetryPolicy(), lambda: _httpx_status(429)) == 1, (
            "原生默认竟开始重试 httpx 429——本用例的对照前提失效，需复核 LG-1-2 的说明"
        )
        assert await self._run(readonly_retry_policy(), lambda: _httpx_status(429)) == 3, (
            "业务策略没有重试 httpx 429（期望耗尽 3 次尝试）——分类没驱动框架行为"
        )

    @pytest.mark.asyncio
    async def test_unknown_error_is_retried_by_framework_default_but_not_by_business_policy(self):
        assert await self._run(RetryPolicy(), lambda: _UnknownError("boom")) == 3, (
            "原生默认对未知异常不再重试 ⇒ 与 LG-1-2 的差异表不符，需复核"
        )
        assert await self._run(readonly_retry_policy(), lambda: _UnknownError("boom")) == 1, (
            "业务策略对未知异常重试了——未知异常多为缺陷，重试换不来修复"
        )
