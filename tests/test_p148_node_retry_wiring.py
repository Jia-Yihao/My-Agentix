"""LG-1-1 验收：6 个只读节点接 LangGraph 原生 ``retry_policy``。

## 验收口径（指导文档 §4.2 LG-1-1「怎么算过」）

1. **注入 429 后节点内重试一次成功**——用 ``readonly_retry_policy()``（LG-0-3 集中点产出）
   在真实 ``StateGraph`` 上跑一次「第一次抛 429、第二次成功」的节点，断言
   结果为成功且**恰好执行两次**（框架侧重试，不在业务节点里写循环）。
2. **``execute_task`` / ``code_agent`` 的注册里没有 ``retry_policy``**——整节点重跑会
   重放工具副作用，属指导文档铁律，本文件把它钉成用例（flag-on 时仍为 ``None``）。
3. **flag-off 逐位等价**（``CONTRIBUTING.md` 的工程约定` 约定 1）：``NODE_RETRY_ENABLED=false``（默认）时
   注册处**完全不传** ``retry_policy``，十个业务节点读到的策略全为 ``None``。

## 一条必须登记的不利事实（LG-1-2 已收口）

LangGraph 原生 ``default_retry_on`` 对 ``httpx.HTTPStatusError`` **只认 5xx**
（源码里写死 ``500 <= status_code < 600``），所以「HTTP 429 以 httpx 状态错误的形态
抛出来」时原生默认**不重试**；而 ``openai.RateLimitError`` 不是 ``HTTPStatusError``
子类，会落到默认的「未知即重试」分支 ⇒ 被重试。真源应当是业务分类函数
``is_retryable``（``agentix/domain/policies/error_classification.py``
把 ``429`` / ``rate limit`` 明确列为可重试）——LG-1-2 已把它接成
``readonly_retry_policy()`` 的 ``retry_on``，接线处从此按业务口径判定。
本文件保留原生默认的**框架事实**（``TestRetryOnSourceOfTruthGap``）与接线处的
**业务读数**（401 一次即抛）作对照，LG-1-2 的验收见
``tests/test_p149_retry_on_business.py``。

判据不依赖网络：全部用本地构造的异常与内存图。
"""

from __future__ import annotations

import httpx
import openai
import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy, default_retry_on

from agentix.config import settings
from agentix.domain.policies import is_retryable
from agentix.orchestration.langgraph import lg_builder
from agentix.orchestration.langgraph.policies import readonly_retry_policy

#: LG-1-1 点名的 6 个只读节点（真源：指导文档 §4.2 LG-1-1「做什么」行）。
READONLY_RETRY_NODES = (
    "analyze_and_route_query",
    "respond_to_general_query",
    "create_research_plan",
    "create_kb_query",
    "create_image_query",
    "create_file_query",
)

#: 明确**不给**重试的节点：窗口化与追问节点不在 LG-1-1 清单里；后两个整节点重放工具副作用。
NEVER_RETRY_NODES = ("message_window", "get_additional_info", "execute_task", "code_agent")


def _rate_limit_429() -> Exception:
    """构造一个真实的 429：``openai.RateLimitError``（SDK 侧限流的实际异常形态）。"""
    response = httpx.Response(
        429,
        request=httpx.Request("POST", "http://127.0.0.1:1/v1/chat/completions"),
        text="Too Many Requests",
    )
    return openai.RateLimitError("Error code 429: rate limit exceeded", response=response, body=None)


def _auth_401() -> Exception:
    """鉴权失败（业务口径明确**不可重试**）的真实异常形态。"""
    response = httpx.Response(401, request=httpx.Request("POST", "http://127.0.0.1:1/v1/chat/completions"))
    return openai.AuthenticationError("Error code 401: invalid api key", response=response, body=None)


def _httpx_status_429() -> httpx.HTTPStatusError:
    """构造 HTTP 429 的**另一条通路**：裸 ``httpx.HTTPStatusError``（原生默认不认）。"""
    request = httpx.Request("POST", "http://127.0.0.1:1/v1/chat/completions")
    response = httpx.Response(429, request=request)
    return httpx.HTTPStatusError("429 Too Many Requests", request=request, response=response)


def _retry_policies_of(compiled, node_name: str) -> tuple:
    """读编译后节点上的重试策略（langgraph 把它存成元组，未传则为 ``None``）。"""
    node = compiled.nodes[node_name]
    return (node.retry_policy or ()) if node.retry_policy is not None else ()


class TestFlagOffIsBitIdentical:
    """约定 1：默认关 ⇒ 十个节点读到的策略全为 ``None``（完全不传）。"""

    def test_default_flag_is_off(self):
        assert settings.NODE_RETRY_ENABLED is False, (
            "NODE_RETRY_ENABLED 默认必须为 False（工程约定 1）；"
            "若确有理由改成 True，须先改本用例并说明部署面"
        )

    def test_no_node_carries_retry_policy_when_off(self):
        compiled = lg_builder.build_graph(None)
        with_policy = [
            name for name in READONLY_RETRY_NODES + NEVER_RETRY_NODES if _retry_policies_of(compiled, name)
        ]
        assert with_policy == [], f"flag-off 时这些节点竟然带上了 retry_policy：{with_policy}"

    def test_flag_off_node_set_unchanged(self):
        """防空真：上面那条必须真的看到 10 个节点，否则"全为 None"是假绿。"""
        compiled = lg_builder.build_graph(None)
        for name in READONLY_RETRY_NODES + NEVER_RETRY_NODES:
            assert name in compiled.nodes, f"编译图里没有节点 {name}，本文件的判据会空跑"


class TestFlagOnWiring:
    """LG-1-1 覆盖面：只给 6 个只读节点，且策略来自 policies.py。"""

    @pytest.fixture
    def compiled_on(self):
        original = settings.NODE_RETRY_ENABLED
        settings.NODE_RETRY_ENABLED = True
        try:
            yield lg_builder.build_graph(None)
        finally:
            settings.NODE_RETRY_ENABLED = original

    def test_six_readonly_nodes_carry_the_policies_factory_output(self, compiled_on):
        expected = readonly_retry_policy()
        for name in READONLY_RETRY_NODES:
            policies = _retry_policies_of(compiled_on, name)
            assert policies == (expected,), (
                f"{name} 的 retry_policy 不是 policies.readonly_retry_policy() 的产出："
                f"实得 {policies}"
            )

    def test_side_effect_and_unlisted_nodes_never_carry_retry(self, compiled_on):
        """验收 ②：flag 打开时，execute_task / code_agent 仍必须没有重试策略。"""
        offenders = [name for name in NEVER_RETRY_NODES if _retry_policies_of(compiled_on, name)]
        assert offenders == [], (
            "这些节点被挂了节点级重试——整节点重跑会重放工具副作用"
            f"（指导文档铁律 / LG-1-1 验收 ②）：{offenders}"
        )

    def test_policy_is_native_defaults_not_a_copied_number_set(self, compiled_on):
        """策略**数值**必须逐项等于原生默认（LG-1-3 的基准），不是抄来的数字。

        LG-1-2 起 ``retry_on`` 是业务分类 ``is_retryable``，是唯一允许的差异项。
        """
        from agentix.domain.policies.retry import is_retryable

        policy = _retry_policies_of(compiled_on, "create_kb_query")[0]
        assert policy.retry_on is is_retryable, f"retry_on 未接业务分类：{policy.retry_on!r}"
        assert policy._replace(retry_on=RetryPolicy().retry_on) == RetryPolicy(), (
            f"数值默认值偏离原生：实得 {policy._asdict()}"
        )


class TestNativeRetryActuallyFires:
    """验收 ①：注入 429 ⇒ 节点内**由框架**重试一次并成功。"""

    @pytest.mark.asyncio
    async def test_429_first_attempt_then_success(self):
        calls = []

        async def flaky(state: dict) -> dict:
            calls.append(1)
            if len(calls) == 1:
                raise _rate_limit_429()
            return {"v": state.get("v", 0) + 1}

        builder = StateGraph(dict)
        builder.add_node("flaky", flaky, retry_policy=readonly_retry_policy())
        builder.add_edge(START, "flaky")
        builder.add_edge("flaky", END)

        result = await builder.compile().ainvoke({"v": 10})
        assert result == {"v": 11}, f"节点重试后未拿到成功结果：{result}"
        assert len(calls) == 2, f"期望「首次 429 + 重试一次成功」共 2 次调用，实得 {len(calls)} 次"

    @pytest.mark.asyncio
    async def test_framework_default_retries_auth_error_business_policy_does_not(self):
        """原生默认把 401 重试用尽；业务策略（``is_retryable``）1 次即抛。

        左边是**框架事实**（``openai.AuthenticationError`` 既不是 ``httpx.HTTPStatusError``
        也不在原生「明确不重试」清单里 ⇒ 落到 ``return True``），右边是 LG-1-2 落地后
        接线处的真实读数。两者放在一起，任何一侧变化都能立刻看出是哪边动了。
        """
        async def _run(policy):
            calls = []

            async def always_auth_error(state: dict) -> dict:
                calls.append(1)
                raise _auth_401()

            builder = StateGraph(dict)
            builder.add_node("auth", always_auth_error, retry_policy=policy)
            builder.add_edge(START, "auth")
            builder.add_edge("auth", END)
            with pytest.raises(openai.AuthenticationError):
                await builder.compile().ainvoke({"v": 1})
            return len(calls)

        assert is_retryable(_auth_401()) is False, (
            "is_retryable 对 401 判可重试——业务分类口径漂移，本用例的对比前提失效"
        )
        framework_calls = await _run(RetryPolicy())
        assert framework_calls == 3, (
            f"原生 RetryPolicy() 对 401 尝试 {framework_calls} 次（期望 3）。"
            "框架默认行为变了 ⇒ 复核 LG-1-2 的动因说明与 test_p149 的对比基准"
        )
        business_calls = await _run(readonly_retry_policy())
        assert business_calls == 1, (
            f"业务策略对 401 尝试 {business_calls} 次（期望 1 次即抛）。"
            "retry_on 没接上 is_retryable ⇒ 鉴权失败会被无意义重试"
        )

    @pytest.mark.asyncio
    async def test_retry_gives_up_after_max_attempts(self):
        calls = []

        async def always_429(state: dict) -> dict:
            calls.append(1)
            raise _rate_limit_429()

        builder = StateGraph(dict)
        builder.add_node("always", always_429, retry_policy=readonly_retry_policy())
        builder.add_edge(START, "always")
        builder.add_edge("always", END)

        with pytest.raises(openai.RateLimitError):
            await builder.compile().ainvoke({"v": 1})
        assert len(calls) == 3, (
            f"max_attempts=3 应当总共尝试 3 次（含首跑），实得 {len(calls)} 次——"
            "次数口径变了要同步 LG-1-3 与文档 §3.6 的默认值说明"
        )


class TestRetryOnSourceOfTruthGap:
    """登记 LG-1-2 的动因：原生默认与业务分类对同一批异常判定不同。"""

    @pytest.mark.parametrize(
        "exc_factory",
        [_rate_limit_429, _httpx_status_429],
        ids=["openai.RateLimitError", "httpx.HTTPStatusError(429)"],
    )
    def test_business_classification_says_retryable(self, exc_factory):
        exc = exc_factory()
        assert is_retryable(exc) is True, f"is_retryable 对 {type(exc).__name__} 判不可重试，口径漂移"

    def test_native_default_disagrees_on_httpx_429(self):
        """``default_retry_on`` 对 ``httpx.HTTPStatusError`` 只认 5xx ⇒ 429 不重试。

        这不是 bug 而是**分层事实**：一旦某条链路把 429 以 httpx 状态错误抛出，
        挂原生默认策略的节点不会重试。LG-1-2 接上 ``retry_on=is_retryable`` 后
        两条通路才统一。
        """
        assert default_retry_on(_httpx_status_429()) is False, (
            "原生 default_retry_on 竟已开始重试 httpx 429——框架行为变了，"
            "需同步本用例与 LG-1-2 的必要性说明"
        )
        assert default_retry_on(_rate_limit_429()) is True, (
            "openai.RateLimitError 不再被原生默认可重试 ⇒ LG-1-1 验收 ① 的前提失效，需复核"
        )
