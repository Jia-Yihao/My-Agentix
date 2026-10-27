"""LG-3-2 验收：主图节点的原生超时（langgraph 1.2 ``TimeoutPolicy``）。

## 覆盖与验收口径（指导文档 §4.4 LG-3-2「怎么算过」）

1. **关时逐位等价**：``NODE_TIMEOUT_SECONDS=0``（默认）时编译出的主图里每个节点
   ``timeout`` 都是 ``None``——即 ``add_node`` **完全不传**该形参；并由一条静态
   AST 守卫钉住「10 个 ``add_node`` 全部 ``**node_timeout_kwargs``」，因为漏接一个
   节点不会报错，只会静默没有超时。
2. **开时超时节点抛 ``NodeTimeoutError`` 且不被吞掉**：同节点挂 ``timeout`` +
   业务 ``retry_policy``，慢节点只执行 **1 次**（``NodeTimeoutError`` 已进领域不可
   重试分类，不会「超时→重试→再超时」）；异常能被原生 ``error_handler`` 接住，
   整轮以可恢复状态结束而不是静默失败。
3. **边界（``AGENTS.md`` 纪律 6 ②）**：超时靠 asyncio 协作式取消——只在节点
   **让出事件循环**的时刻生效。两条特征化用例把它钉死：整段不让出的协程杀不掉；
   阻塞后才让出的协程能杀，但实际耗时是阻塞时长而不是上限（超时不可抢占）。
   同步节点更早就被拦下：``compile().validate()`` 直接抛 ``ValueError``。
4. **策略集中**：超时策略只从 ``policies.node_timeout`` 出（``0``/负数一律
   ``None``），``lg_builder`` 内不出现 ``TimeoutPolicy(...)`` 字面构造。
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import time
from typing import Any, Dict, List, TypedDict

import pytest
from langgraph.errors import NodeError, NodeTimeoutError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, TimeoutPolicy
from pydantic import ValidationError

from agentix.config import settings
from agentix.domain.policies.error_classification import (
    NON_RETRYABLE_HINTS,
    is_retryable,
)
from agentix.orchestration.langgraph import lg_builder
from agentix.orchestration.langgraph.policies import node_timeout, readonly_retry_policy

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
LG_BUILDER_PATH = REPO_ROOT / "agentix" / "orchestration" / "langgraph" / "lg_builder.py"

#: 主图注册的 10 个节点（真源：``build_graph`` 的 add_node 块；编译图另含 ``__start__``）。
MAIN_GRAPH_NODES = (
    "message_window",
    "analyze_and_route_query",
    "respond_to_general_query",
    "get_additional_info",
    "create_research_plan",
    "create_image_query",
    "create_file_query",
    "create_kb_query",
    "execute_task",
    "code_agent",
)


class _State(TypedDict, total=False):
    x: int
    recovered: str


def _run(coro):
    return asyncio.run(coro)


class TestNodeTimeoutPolicyFactory:
    """``policies.node_timeout`` 是唯一构造点；非正一律「关」。"""

    @pytest.mark.parametrize(
        ("run_timeout", "idle_timeout"),
        [(None, None), (0, 0), (0.0, 0.0), (-1, None), (None, -5)],
        ids=["both-none", "zero", "zero-float", "negative-run", "negative-idle"],
    )
    def test_non_positive_means_off(self, run_timeout, idle_timeout):
        assert node_timeout(run_timeout=run_timeout, idle_timeout=idle_timeout) is None

    def test_positive_builds_native_policy(self):
        policy = node_timeout(run_timeout=12.5)
        assert isinstance(policy, TimeoutPolicy)
        assert policy.run_timeout == 12.5
        assert policy.idle_timeout is None
        assert policy.refresh_on == "auto"

    def test_idle_only_is_enabled(self):
        policy = node_timeout(idle_timeout=3)
        assert isinstance(policy, TimeoutPolicy)
        assert policy.idle_timeout == 3
        assert policy.run_timeout is None

    def test_negative_side_is_dropped_not_passed_through(self):
        """单侧为负时另一侧仍生效，负的一侧按 None 处理（不把负值交给框架）。"""
        policy = node_timeout(run_timeout=5, idle_timeout=-2)
        assert policy is not None
        assert policy.run_timeout == 5
        assert policy.idle_timeout is None


class TestSettingsKey:
    def test_default_is_off(self):
        assert settings.NODE_TIMEOUT_SECONDS == 0
        assert isinstance(settings.NODE_TIMEOUT_SECONDS, float)

    def test_non_negative_constraint(self):
        with pytest.raises(ValidationError):
            settings.__class__(NODE_TIMEOUT_SECONDS=-1)


class TestMainGraphWiring:
    """关时完全不传；开时每个节点都挂上同一个策略。"""

    def test_default_off_passes_no_timeout_at_all(self):
        compiled = lg_builder.build_graph(None)
        for name in MAIN_GRAPH_NODES:
            assert name in compiled.nodes, f"编译图里没有节点 {name}"
            assert getattr(compiled.nodes[name], "timeout", None) is None, (
                f"{name} 在开关关闭时仍带 timeout——flag-off 必须与基线逐位等价"
            )

    def test_helper_returns_empty_dict_when_off(self):
        original = settings.NODE_TIMEOUT_SECONDS
        settings.NODE_TIMEOUT_SECONDS = 0
        try:
            assert lg_builder._node_timeout_kwargs() == {}
        finally:
            settings.NODE_TIMEOUT_SECONDS = original

    def test_helper_returns_policy_when_on(self):
        original = settings.NODE_TIMEOUT_SECONDS
        settings.NODE_TIMEOUT_SECONDS = 9.5
        try:
            kwargs = lg_builder._node_timeout_kwargs()
            assert set(kwargs) == {"timeout"}
            assert isinstance(kwargs["timeout"], TimeoutPolicy)
            assert kwargs["timeout"].run_timeout == 9.5
        finally:
            settings.NODE_TIMEOUT_SECONDS = original

    def test_on_attaches_policy_to_every_node(self):
        original = settings.NODE_TIMEOUT_SECONDS
        settings.NODE_TIMEOUT_SECONDS = 12.5
        try:
            compiled = lg_builder.build_graph(None)
        finally:
            settings.NODE_TIMEOUT_SECONDS = original
        for name in MAIN_GRAPH_NODES:
            policy = getattr(compiled.nodes[name], "timeout", None)
            assert isinstance(policy, TimeoutPolicy), f"{name} 没挂上 timeout：{policy!r}"
            assert policy.run_timeout == 12.5, f"{name} 的 run_timeout 不是配置值"
            assert policy.idle_timeout is None, "本项只配置墙钟 run_timeout，不配 idle_timeout"


def _build_graph_add_node_calls() -> List[ast.Call]:
    tree = ast.parse(LG_BUILDER_PATH.read_text(encoding="utf-8"))
    fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_graph"
    )
    return [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_node"
    ]


def _spread_names(call: ast.Call) -> set:
    return {
        kw.value.id
        for kw in call.keywords
        if kw.arg is None and isinstance(kw.value, ast.Name)
    }


class TestWiringIsNotPartiallyForgotten:
    """静态守卫：漏接一个节点不会报错，只会静默没有超时。"""

    def test_every_add_node_spreads_node_timeout_kwargs(self):
        calls = _build_graph_add_node_calls()
        assert len(calls) == len(MAIN_GRAPH_NODES) == 10, (
            f"build_graph 的 add_node 数是 {len(calls)}，与节点清单 {len(MAIN_GRAPH_NODES)} 不一致"
            "——接线或清单有一方漂移，需同步"
        )
        missing = [call.lineno for call in calls if "node_timeout_kwargs" not in _spread_names(call)]
        assert not missing, (
            f"lg_builder.py 第 {missing} 行的 add_node 没有 `**node_timeout_kwargs`"
            "——该节点在开关打开时不会超时，且不会报错"
        )

    def test_helper_value_comes_from_policies_factory(self):
        """``**`` 展开在 p147 判据 2 里被豁免，所以这里补钉「值的来源」。"""
        tree = ast.parse(LG_BUILDER_PATH.read_text(encoding="utf-8"))
        fn = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_node_timeout_kwargs"
        )
        callees = {
            node.func.id
            for node in ast.walk(fn)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "node_timeout" in callees, (
            "_node_timeout_kwargs 必须经 policies.node_timeout 构造策略"
            "（LG-0-3 集中点），不得在文件内联 TimeoutPolicy(...)"
        )
        assert not any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "TimeoutPolicy"
            for node in ast.walk(fn)
        ), "lg_builder.py 不允许内联构造 TimeoutPolicy"


async def _run_slow_node(*, run_timeout: float, block: float = 5.0, retry_policy=None, error_handler=None):
    calls: List[int] = []

    async def slow(state: _State) -> _State:
        calls.append(1)
        await asyncio.sleep(block)
        return {}

    builder = StateGraph(_State)
    extra: Dict[str, Any] = {}
    if retry_policy is not None:
        extra["retry_policy"] = retry_policy
    if error_handler is not None:
        extra["error_handler"] = error_handler
    builder.add_node("slow", slow, timeout=TimeoutPolicy(run_timeout=run_timeout), **extra)
    builder.add_edge(START, "slow")
    builder.add_edge("slow", END)
    return builder.compile(), calls


class TestTimeoutDrivesRealBehavior:
    """开时：抛 ``NodeTimeoutError``，不被重试吞掉，且能被 ``error_handler`` 接住。"""

    def test_raises_native_node_timeout_error(self):
        async def main():
            compiled, calls = await _run_slow_node(run_timeout=0.1)
            with pytest.raises(NodeTimeoutError) as excinfo:
                await compiled.ainvoke({"x": 1})
            return excinfo.value, calls

        exc, calls = _run(main())
        assert exc.node == "slow"
        assert exc.run_timeout == 0.1
        assert exc.kind == "run"
        assert len(calls) == 1, "未被重试时慢节点应只执行一次"

    def test_business_retry_policy_does_not_retry_timeout(self):
        """验收第 ④ 条：不排除 ``NodeTimeoutError`` 就会「超时→重试→再超时」。"""

        async def main():
            compiled, calls = await _run_slow_node(
                run_timeout=0.1, retry_policy=readonly_retry_policy()
            )
            with pytest.raises(NodeTimeoutError):
                await compiled.ainvoke({"x": 1})
            return calls

        calls = _run(main())
        assert len(calls) == 1, (
            f"慢节点执行了 {len(calls)} 次——业务 retry_policy 仍在重试 NodeTimeoutError，"
            "超时耗时被按尝试次数放大"
        )

    def test_error_handler_receives_the_timeout_error(self):
        seen: Dict[str, Any] = {}

        def handler(state: _State, error: NodeError) -> Command:
            seen["node"] = error.node
            seen["exc"] = type(error.error).__name__
            return Command(update={"recovered": type(error.error).__name__})

        async def main():
            compiled, calls = await _run_slow_node(
                run_timeout=0.1,
                retry_policy=readonly_retry_policy(),
                error_handler=handler,
            )
            return await compiled.ainvoke({"x": 1}), calls

        state, calls = _run(main())
        assert seen == {"node": "slow", "exc": "NodeTimeoutError"}, seen
        assert state.get("recovered") == "NodeTimeoutError", (
            f"error_handler 的 Command 更新没落地：{state!r}——整轮会静默失败"
        )
        assert len(calls) == 1

    def test_sync_node_with_timeout_is_rejected_at_compile(self):
        def sync_node(state: _State) -> _State:  # pragma: no cover - 只验证编译期拒绝
            return {}

        builder = StateGraph(_State)
        builder.add_node("sync", sync_node, timeout=TimeoutPolicy(run_timeout=1))
        builder.add_edge(START, "sync")
        builder.add_edge("sync", END)
        with pytest.raises(ValueError, match="async"):
            builder.compile()


class TestCooperativeCancellationBoundary:
    """纪律 6 ②：超时只在节点让出事件循环的时刻生效——不是抢占式杀伤。"""

    def test_coroutine_that_never_yields_completes_anyway(self):
        block = 0.4
        cap = 0.05

        async def never_yields(state: _State) -> _State:
            time.sleep(block)  # 故意阻塞事件循环，全程不让出
            return {"x": 2}

        async def main():
            builder = StateGraph(_State)
            builder.add_node("n", never_yields, timeout=TimeoutPolicy(run_timeout=cap))
            builder.add_edge(START, "n")
            builder.add_edge("n", END)
            compiled = builder.compile()
            started = time.perf_counter()
            result = await compiled.ainvoke({"x": 1})
            return result, time.perf_counter() - started

        result, elapsed = _run(main())
        assert result.get("x") == 2, "不让出事件循环的协程本就不会被取消——本用例是特征化，不是期望"
        assert elapsed >= block * 0.75, f"实际耗时 {elapsed:.2f}s，阻塞时长 {block}s"

    def test_blocking_then_yielding_node_is_killed_after_the_block(self):
        block = 0.4
        cap = 0.05

        async def block_then_yield(state: _State) -> _State:
            time.sleep(block)
            await asyncio.sleep(0)  # 让出：超时在这里才判得出来
            return {"x": 2}

        async def main():
            builder = StateGraph(_State)
            builder.add_node("n", block_then_yield, timeout=TimeoutPolicy(run_timeout=cap))
            builder.add_edge(START, "n")
            builder.add_edge("n", END)
            compiled = builder.compile()
            timeout_error = None
            started = time.perf_counter()
            try:
                await compiled.ainvoke({"x": 1})
            except NodeTimeoutError as exc:
                timeout_error = exc
            return timeout_error, time.perf_counter() - started

        timeout_error, elapsed = _run(main())
        assert timeout_error is not None, "让出事件循环后仍未被超时截断"
        assert elapsed >= block * 0.75, (
            f"实际耗时 {elapsed:.2f}s 小于阻塞时长 {block}s——超时若真能抢占，"
            "这条断言就会失败，说明边界说明已过时"
        )
        assert timeout_error.elapsed >= block * 0.75


class TestClassificationExcludesTimeout:
    """验收第 ④ 条的另一半：分类层（唯一真源）把 ``NodeTimeoutError`` 判为不可重试。"""

    def test_hint_is_present(self):
        assert "nodetimeouterror" in NON_RETRYABLE_HINTS

    def test_is_retryable_excludes_node_timeout(self):
        exc = NodeTimeoutError("slow", 0.11, kind="run", run_timeout=0.1)
        assert is_retryable(exc) is False

    def test_business_retry_on_excludes_node_timeout(self):
        exc = NodeTimeoutError("slow", 0.11, kind="run", run_timeout=0.1)
        assert readonly_retry_policy().retry_on(exc) is False

    def test_plain_timeout_error_is_still_retryable(self):
        """新提示词不得误伤内置 ``TimeoutError``（p149 的期望列仍成立）。"""
        assert is_retryable(TimeoutError("request timed out")) is True
