"""Phase H·W3 运行时治理验收（p97）。

覆盖规划 §4 W3 五项：
1. 准入三态：flag-off 直通零记录、放行/排队/拒绝、显式上限回落
   CONTROL_PLANE 上限、排队等位有界（超时仍拒）、队列深度输入、
   拒绝经全局处理器映射 429；
2. 重试预算：桶耗尽/分层记账、RequestScope 跨层共享与 scope 隔离、
   task/tool/retrieval 三层 flag-on 停重试、分类审计（仅瞬时错误可重试）、
   flag-off 各层基线行为逐位等价；
3. 自适应超时：flag-off = spec 原值（文案字节等价）、flag-on 裁剪到
   剩余 run 预算、无 deadline 回原值、scope deadline 助手；
4. 隔离舱 + 共享客户端：0=不限直通、按工具归池、同池串行/异池不串扰、
   超时释放池位、executor 接线在位、共享 client 复用与关停重建；
5. 降级阶梯：classify_load 纯函数分级、flag-off 恒 L0、按级别甩后台
   （evolution 最先、未知类型保守不甩）、上行迁移 record_degraded 留痕、
   L6 经准入礼貌拒绝 429、task_queue 消费侧甩负荷。

运行：./.venv/Scripts/python.exe -m pytest tests/test_p97_runtime_control.py -q
"""
import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentix.config import settings


# ===========================================================================
# 隔离夹具（W3 新增单例全部可 reset；lag/平面在测试间归零）
# ===========================================================================


@pytest.fixture(autouse=True)
def _w3_isolate(monkeypatch):
    from agentix.observability import metrics as _m
    from agentix.performance.admission import reset_admission_controller
    from agentix.performance.load_shedder import reset_load_shedder
    from agentix.reliability.bulkhead import reset_bulkheads
    from agentix.reliability.retry_budget import reset_retry_budget
    from agentix.runtime.control_plane import reset_control_plane

    for reset in (
        reset_admission_controller,
        reset_load_shedder,
        reset_retry_budget,
        reset_bulkheads,
        reset_control_plane,
    ):
        reset()
    _m.record_event_loop_lag(0.0)
    yield
    for reset in (
        reset_admission_controller,
        reset_load_shedder,
        reset_retry_budget,
        reset_bulkheads,
        reset_control_plane,
    ):
        reset()
    _m.record_event_loop_lag(0.0)


def _sample(name: str, **labels) -> float:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, labels) or 0.0


def _no_downstream_impairment(monkeypatch):
    """隔离下游熔断输入（live registry 可能带其他测试留下的 OPEN 态）。"""
    import agentix.performance.load_shedder as ls

    monkeypatch.setattr(ls, "_impaired_downstreams", lambda: 0)


# ===========================================================================
# 1. 准入三态
# ===========================================================================


class TestAdmission:
    @pytest.mark.asyncio
    async def test_flag_off_passes_even_at_capacity(self, monkeypatch):
        from agentix.performance.admission import admission_wait
        from agentix.runtime.control_plane import get_control_plane

        monkeypatch.setattr(settings, "ADMISSION_CONTROL_ENABLED", False)
        monkeypatch.setattr(settings, "ADMISSION_MAX_CONCURRENT_RUNS", 1)
        monkeypatch.setattr(settings, "CONTROL_PLANE_MAX_CONCURRENCY", 0)
        plane = get_control_plane()
        plane.spawn("agent")  # 到顶

        before = _sample("admission_decisions_total", state="rejected")
        assert await admission_wait(kind="test") is None
        assert _sample("admission_decisions_total", state="rejected") == before

    @pytest.mark.asyncio
    async def test_admits_under_capacity(self, monkeypatch):
        from agentix.performance.admission import admission_wait
        from agentix.runtime.control_plane import get_control_plane

        monkeypatch.setattr(settings, "ADMISSION_CONTROL_ENABLED", True)
        monkeypatch.setattr(settings, "ADMISSION_MAX_CONCURRENT_RUNS", 2)
        monkeypatch.setattr(settings, "CONTROL_PLANE_MAX_CONCURRENCY", 0)
        get_control_plane().spawn("agent")

        before = _sample("admission_decisions_total", state="admitted")
        assert await admission_wait(kind="test") is None
        assert _sample("admission_decisions_total", state="admitted") == before + 1

    @pytest.mark.asyncio
    async def test_rejects_at_capacity_queue_disabled(self, monkeypatch):
        from agentix.performance.admission import admission_wait
        from agentix.runtime.control_plane import AdmissionDenied, get_control_plane

        monkeypatch.setattr(settings, "ADMISSION_CONTROL_ENABLED", True)
        monkeypatch.setattr(settings, "ADMISSION_MAX_CONCURRENT_RUNS", 1)
        monkeypatch.setattr(settings, "ADMISSION_MAX_QUEUE", 0)
        monkeypatch.setattr(settings, "CONTROL_PLANE_MAX_CONCURRENCY", 0)
        get_control_plane().spawn("agent")

        with pytest.raises(AdmissionDenied, match="queue disabled"):
            await admission_wait(kind="test")

    @pytest.mark.asyncio
    async def test_explicit_limit_overrides_control_plane_fallback(self, monkeypatch):
        """ADMISSION_MAX_CONCURRENT_RUNS>0 时不受 CONTROL_PLANE（0=不限）影响。"""
        from agentix.performance.admission import admission_wait
        from agentix.runtime.control_plane import AdmissionDenied, get_control_plane

        monkeypatch.setattr(settings, "ADMISSION_CONTROL_ENABLED", True)
        monkeypatch.setattr(settings, "ADMISSION_MAX_CONCURRENT_RUNS", 1)
        monkeypatch.setattr(settings, "CONTROL_PLANE_MAX_CONCURRENCY", 0)
        get_control_plane().spawn("agent")

        with pytest.raises(AdmissionDenied):
            await admission_wait(kind="test")

    @pytest.mark.asyncio
    async def test_falls_back_to_control_plane_limit(self, monkeypatch):
        """ADMISSION_MAX_CONCURRENT_RUNS=0 → 回落 CONTROL_PLANE_MAX_CONCURRENCY。"""
        from agentix.performance.admission import admission_wait
        from agentix.runtime.control_plane import (
            AdmissionDenied,
            get_control_plane,
            reset_control_plane,
        )

        monkeypatch.setattr(settings, "ADMISSION_CONTROL_ENABLED", True)
        monkeypatch.setattr(settings, "ADMISSION_MAX_CONCURRENT_RUNS", 0)
        monkeypatch.setattr(settings, "CONTROL_PLANE_MAX_CONCURRENCY", 1)
        reset_control_plane()
        plane = get_control_plane()
        plane.spawn("agent")  # 平面闸放行（active 0→1）

        with pytest.raises(AdmissionDenied, match="active runs"):
            await admission_wait(kind="test")

    @pytest.mark.asyncio
    async def test_queue_waits_then_admits(self, monkeypatch):
        from agentix.performance.admission import admission_wait, queue_capacity, queue_depth
        from agentix.runtime.control_plane import get_control_plane

        monkeypatch.setattr(settings, "ADMISSION_CONTROL_ENABLED", True)
        monkeypatch.setattr(settings, "ADMISSION_MAX_CONCURRENT_RUNS", 1)
        monkeypatch.setattr(settings, "ADMISSION_MAX_QUEUE", 1)
        monkeypatch.setattr(settings, "CONTROL_PLANE_MAX_CONCURRENCY", 0)
        run = get_control_plane().spawn("agent")
        assert queue_capacity() == 1

        queued_before = _sample("admission_decisions_total", state="queued")
        waiter = asyncio.create_task(admission_wait(kind="test"))
        await asyncio.sleep(0.12)  # > 2 个轮询间隔
        assert not waiter.done()
        assert queue_depth() == 1  # 等位深度是负载阶梯输入

        get_control_plane().finish(run.run_id)
        assert await asyncio.wait_for(waiter, timeout=2.0) is None
        assert queue_depth() == 0
        assert _sample("admission_decisions_total", state="queued") == queued_before + 1

    @pytest.mark.asyncio
    async def test_queue_full_rejects_second_waiter(self, monkeypatch):
        from agentix.performance.admission import admission_wait
        from agentix.runtime.control_plane import AdmissionDenied, get_control_plane

        monkeypatch.setattr(settings, "ADMISSION_CONTROL_ENABLED", True)
        monkeypatch.setattr(settings, "ADMISSION_MAX_CONCURRENT_RUNS", 1)
        monkeypatch.setattr(settings, "ADMISSION_MAX_QUEUE", 1)
        monkeypatch.setattr(settings, "CONTROL_PLANE_MAX_CONCURRENCY", 0)
        run = get_control_plane().spawn("agent")

        first = asyncio.create_task(admission_wait(kind="test"))
        await asyncio.sleep(0.1)
        assert not first.done()
        with pytest.raises(AdmissionDenied, match="queue full"):
            await admission_wait(kind="test")

        get_control_plane().finish(run.run_id)
        await asyncio.wait_for(first, timeout=2.0)

    @pytest.mark.asyncio
    async def test_enqueue_timeout_still_rejects(self, monkeypatch):
        import agentix.performance.admission as adm

        from agentix.performance.admission import admission_wait
        from agentix.runtime.control_plane import AdmissionDenied, get_control_plane

        monkeypatch.setattr(settings, "ADMISSION_CONTROL_ENABLED", True)
        monkeypatch.setattr(settings, "ADMISSION_MAX_CONCURRENT_RUNS", 1)
        monkeypatch.setattr(settings, "ADMISSION_MAX_QUEUE", 1)
        monkeypatch.setattr(settings, "CONTROL_PLANE_MAX_CONCURRENCY", 0)
        monkeypatch.setattr(adm, "ENQUEUE_TIMEOUT_S", 0.15)
        get_control_plane().spawn("agent")  # 永不释放

        with pytest.raises(AdmissionDenied, match="queue timeout"):
            await admission_wait(kind="test")

    @pytest.mark.asyncio
    async def test_denied_maps_to_429_and_plain_error_stays_500(self, monkeypatch):
        from agentix.main import global_exception_handler
        from agentix.runtime.control_plane import AdmissionDenied

        req = SimpleNamespace(method="POST", url="http://testserver/v1/chat")
        resp = await global_exception_handler(
            req, AdmissionDenied("admission denied: queue full")
        )
        assert resp.status_code == 429
        assert b"Too Many Requests" in resp.body

        resp2 = await global_exception_handler(req, RuntimeError("boom"))
        assert resp2.status_code == 500


# ===========================================================================
# 2. 重试预算 + 分类审计
# ===========================================================================


class TestRetryBudget:
    def test_exhaustion_and_layer_stats(self):
        from agentix.reliability.retry_budget import RetryBudget

        budget = RetryBudget(2)
        assert budget.try_consume(layer="task") is True
        assert budget.try_consume(layer="task") is True
        assert budget.try_consume(layer="tool") is False  # 桶共享：耗尽即停
        stats = budget.stats()
        assert stats["remaining"] == 0
        assert stats["consumed"] == 2
        assert stats["layer_task"] == 2

    @pytest.mark.asyncio
    async def test_shared_across_layers_within_request_scope(self):
        from agentix.application.services.request_scope import request_scope
        from agentix.reliability.retry_budget import ensure_retry_budget

        async with request_scope() as scope:
            b1 = ensure_retry_budget()
            assert b1 is scope.retry_budget
            for _ in range(int(settings.RETRY_BUDGET_TOKENS)):
                assert b1.try_consume(layer="task")
            # 同一 scope 内另一层取到同一个桶（跨层共享 = 防 3×3×3）
            assert ensure_retry_budget() is b1
            assert ensure_retry_budget().try_consume(layer="tool") is False

    @pytest.mark.asyncio
    async def test_scope_isolation(self):
        from agentix.application.services.request_scope import request_scope
        from agentix.reliability.retry_budget import ensure_retry_budget

        async with request_scope():
            b1 = ensure_retry_budget()
            b1.try_consume(layer="task")
        async with request_scope() as s2:
            b2 = ensure_retry_budget()
            assert b2 is not b1
            assert s2.retry_budget is b2

    @pytest.mark.asyncio
    async def test_task_layer_stops_when_budget_exhausted(self, monkeypatch):
        from agentix.application.services.task_retry import TaskSpec, run_task

        monkeypatch.setattr(settings, "RETRY_BUDGET_ENABLED", True)
        monkeypatch.setattr(settings, "RETRY_BUDGET_TOKENS", 1)
        calls = 0

        async def runner(spec):
            nonlocal calls
            calls += 1
            raise ConnectionError("connection reset by peer")

        out = await run_task(
            TaskSpec("t"), runner, max_attempts=3, base_delay=0.01, jitter=False
        )
        assert out.ok is False
        assert calls == 2  # 首次 + 1 枚 token，第二枚耗尽停

    @pytest.mark.asyncio
    async def test_task_layer_flag_off_keeps_full_attempts(self, monkeypatch):
        from agentix.application.services.task_retry import TaskSpec, run_task

        monkeypatch.setattr(settings, "RETRY_BUDGET_ENABLED", False)
        calls = 0

        async def runner(spec):
            nonlocal calls
            calls += 1
            raise ConnectionError("connection reset by peer")

        out = await run_task(
            TaskSpec("t"), runner, max_attempts=3, base_delay=0.01, jitter=False
        )
        assert out.ok is False
        assert calls == 3  # 基线：不因预算少跑

    def test_retryable_error_audit_contract(self):
        from agentix.reliability.retry_budget import retryable_error

        assert retryable_error(TimeoutError()) is True  # timeout
        assert retryable_error(ConnectionResetError("connection reset")) is True
        assert retryable_error(Exception("upstream 503 overloaded")) is True
        assert retryable_error(ValueError("401 invalid api key")) is False
        assert retryable_error(Exception("403 forbidden for tenant")) is False

    @pytest.mark.asyncio
    async def test_tool_layer_non_retryable_gives_up_immediately(self, monkeypatch):
        from agentix.tools.definitions import ToolSpec
        from agentix.tools.gateway.executor import ToolExecutor

        monkeypatch.setattr(settings, "RETRY_BUDGET_ENABLED", True)
        calls = 0

        async def handler(args):
            nonlocal calls
            calls += 1
            raise ValueError("invalid_request: bad arg")

        executor = ToolExecutor()
        executor.register_handler(
            "p97_audit_tool", handler, spec=ToolSpec(name="p97_audit_tool", description="d")
        )
        result = await executor.execute("p97_audit_tool", {})
        assert result.success is False
        assert calls == 1  # 分类闸：4xx/参数类不重试

    @pytest.mark.asyncio
    async def test_tool_layer_timeout_is_retryable_under_budget(self, monkeypatch):
        from agentix.tools.definitions import ToolSpec
        from agentix.tools.gateway.executor import ToolExecutor

        monkeypatch.setattr(settings, "RETRY_BUDGET_ENABLED", True)
        monkeypatch.setattr(settings, "RETRY_BUDGET_TOKENS", 6)
        calls = 0

        async def handler(args):
            nonlocal calls
            calls += 1
            raise TimeoutError("timed out calling upstream")

        executor = ToolExecutor()
        executor.register_handler(
            "p97_timeout_tool",
            handler,
            spec=ToolSpec(name="p97_timeout_tool", description="d", retryable=True, max_retries=2),
        )
        result = await executor.execute("p97_timeout_tool", {})
        assert result.success is False
        assert calls == 3  # 瞬时错误：预算内重试到上限

    @pytest.mark.asyncio
    async def test_tool_layer_flag_off_retries_any_error(self, monkeypatch):
        from agentix.tools.definitions import ToolSpec
        from agentix.tools.gateway.executor import ToolExecutor

        monkeypatch.setattr(settings, "RETRY_BUDGET_ENABLED", False)
        calls = 0

        async def handler(args):
            nonlocal calls
            calls += 1
            raise ValueError("invalid_request: bad arg")

        executor = ToolExecutor()
        executor.register_handler(
            "p97_base_tool",
            handler,
            spec=ToolSpec(name="p97_base_tool", description="d", retryable=True, max_retries=2),
        )
        await executor.execute("p97_base_tool", {})
        assert calls == 3  # 基线：任意异常都按 spec 重试

    @pytest.mark.asyncio
    async def test_retrieval_layer_classification_gives_up_without_sleep(self, monkeypatch):
        from agentix.retrieval.usecases.chat_service import retrieve_docs_with_retry

        monkeypatch.setattr(settings, "RETRY_BUDGET_ENABLED", True)
        calls = 0

        class _VS:
            index = None

            def similarity_search_with_score(self, question, *, k, filter, fetch_k):
                nonlocal calls
                calls += 1
                raise ValueError("401 invalid api key")

        with pytest.raises(ValueError):
            retrieve_docs_with_retry(_VS(), "q", search_k=4, max_retries=2)
        assert calls == 1  # 分类闸：不进退避睡眠，立即上抛

    @pytest.mark.asyncio
    async def test_retrieval_layer_flag_off_keeps_retries(self, monkeypatch):
        from agentix.retrieval.usecases.chat_service import retrieve_docs_with_retry

        monkeypatch.setattr(settings, "RETRY_BUDGET_ENABLED", False)
        calls = 0

        class _VS:
            index = None

            def similarity_search_with_score(self, question, *, k, filter, fetch_k):
                nonlocal calls
                calls += 1
                raise ValueError("401 invalid api key")

        with pytest.raises(ValueError):
            retrieve_docs_with_retry(_VS(), "q", search_k=4, max_retries=1)
        assert calls == 2  # 基线：分类无关，按 max_retries 重试（含 0.5s 退避）


# ===========================================================================
# 3. 自适应工具超时
# ===========================================================================


class TestAdaptiveToolTimeout:
    def test_scope_deadline_helpers(self):
        from agentix.application.services.request_scope import RequestScope

        scope = RequestScope()
        assert scope.remaining_ms() is None  # 未设 deadline → None（回原行为）
        scope.set_deadline_seconds(0.05)
        remaining = scope.remaining_ms()
        assert remaining is not None and 0 < remaining <= 50
        scope.set_deadline_seconds(0)  # 0/负 = 不设
        scope.deadline_at = None
        assert scope.remaining_ms() is None

    @pytest.mark.asyncio
    async def test_flag_off_uses_spec_timeout_verbatim(self, monkeypatch):
        from agentix.tools.definitions import ToolSpec
        from agentix.tools.gateway.executor import ToolExecutor

        monkeypatch.setattr(settings, "ADAPTIVE_TOOL_TIMEOUT_ENABLED", False)

        async def handler(args):
            await asyncio.sleep(0.15)

        executor = ToolExecutor()
        executor.register_handler(
            "p97_slow_base",
            handler,
            spec=ToolSpec(name="p97_slow_base", description="d", timeout_ms=40),
        )
        result = await executor.execute("p97_slow_base", {})
        assert result.success is False
        assert result.error == "timeout after 40ms"  # 文案字节等价

    @pytest.mark.asyncio
    async def test_flag_on_clips_to_remaining_run_budget(self, monkeypatch):
        from agentix.application.services.request_scope import request_scope
        from agentix.tools.definitions import ToolSpec
        from agentix.tools.gateway.executor import ToolExecutor

        monkeypatch.setattr(settings, "ADAPTIVE_TOOL_TIMEOUT_ENABLED", True)

        async def handler(args):
            await asyncio.sleep(0.3)

        executor = ToolExecutor()
        executor.register_handler(
            "p97_slow_clip",
            handler,
            spec=ToolSpec(name="p97_slow_clip", description="d", timeout_ms=5000),
        )
        async with request_scope() as scope:
            scope.set_deadline_seconds(0.06)  # 剩余 ~60ms ≪ spec 5000ms
            result = await executor.execute("p97_slow_clip", {})
        assert result.success is False
        match = re.match(r"timeout after (\d+)ms", result.error or "")
        assert match, result.error
        assert 1 <= int(match.group(1)) <= 200  # 被裁到 run 剩余预算（±调度抖动）

    @pytest.mark.asyncio
    async def test_flag_on_without_scope_keeps_spec(self, monkeypatch):
        from agentix.tools.gateway.executor import _effective_timeout_ms

        monkeypatch.setattr(settings, "ADAPTIVE_TOOL_TIMEOUT_ENABLED", True)
        # 无活动 RequestScope → None 回落 spec 原值
        assert _effective_timeout_ms(30_000) == 30_000
        monkeypatch.setattr(settings, "ADAPTIVE_TOOL_TIMEOUT_ENABLED", False)
        assert _effective_timeout_ms(1_500) == 1_500


# ===========================================================================
# 4. 隔离舱 + 共享 HTTP 客户端
# ===========================================================================


class TestBulkhead:
    def test_zero_size_pass_through_and_tool_pool_map(self, monkeypatch):
        from agentix.reliability.bulkhead import pool_for_tool

        monkeypatch.setattr(settings, "BULKHEAD_NEO4J_SIZE", 0)
        monkeypatch.setattr(settings, "BULKHEAD_WEB_SEARCH_SIZE", 0)
        assert pool_for_tool("cypher_query") is None  # 0 = 不限 = 不归池
        assert pool_for_tool("search") is None
        assert pool_for_tool("unknown_tool") is None

        monkeypatch.setattr(settings, "BULKHEAD_NEO4J_SIZE", 4)
        monkeypatch.setattr(settings, "BULKHEAD_WEB_SEARCH_SIZE", 2)
        assert pool_for_tool("cypher_query") == "neo4j"
        assert pool_for_tool("predefined_cypher") == "neo4j"
        assert pool_for_tool("search") == "web_search"
        assert pool_for_tool("unknown_tool") is None  # 未声明不入池

    @pytest.mark.asyncio
    async def test_same_pool_serializes_isolated_pools_do_not_block(self, monkeypatch):
        from agentix.reliability.bulkhead import bulkhead_slot

        monkeypatch.setattr(settings, "BULKHEAD_NEO4J_SIZE", 1)
        monkeypatch.setattr(settings, "BULKHEAD_WEB_SEARCH_SIZE", 1)
        order: list[str] = []

        async def hold(tag: str, delay: float):
            async with bulkhead_slot("neo4j"):
                order.append(f"{tag}:in")
                await asyncio.sleep(delay)
                order.append(f"{tag}:out")

        async def web():
            async with bulkhead_slot("web_search"):
                order.append("web:in")

        await asyncio.gather(hold("a", 0.15), hold("b", 0.001), web())
        assert order[0] == "a:in"
        # 同池串行：b 必须等 a 出池
        assert order.index("b:in") > order.index("a:out")
        # 异池不串扰：web 在 a 占池期间即可执行
        assert order.index("web:in") < order.index("a:out")

    @pytest.mark.asyncio
    async def test_pooled_wait_for_timeout_releases_slot(self, monkeypatch):
        from agentix.reliability.bulkhead import bulkhead_slot, pooled_wait_for

        monkeypatch.setattr(settings, "BULKHEAD_NEO4J_SIZE", 1)

        async def slow():
            await asyncio.sleep(1.0)

        with pytest.raises(asyncio.TimeoutError):
            await pooled_wait_for(slow(), 0.05, "neo4j")

        # 池位已释放：后续进入不再被上一次超时占住
        async def quick():
            return "ok"

        assert await pooled_wait_for(quick(), 0.5, "neo4j") == "ok"
        async with bulkhead_slot("neo4j"):
            pass  # 不阻塞即证明归还

    @pytest.mark.asyncio
    async def test_executor_wires_pool_into_attempt(self, monkeypatch):
        import agentix.reliability.bulkhead as bh_mod
        from agentix.tools.definitions import ToolSpec
        from agentix.tools.gateway.executor import ToolExecutor

        seen: dict = {}
        real_pooled = bh_mod.pooled_wait_for

        async def spy(coro, timeout_s, pool):
            seen["pool"] = pool
            return await real_pooled(coro, timeout_s, pool)

        # executor 为函数内 import（每次调用重新绑定）→ 改源模块属性即生效
        monkeypatch.setattr(bh_mod, "pool_for_tool", lambda name: "neo4j")
        monkeypatch.setattr(bh_mod, "pooled_wait_for", spy)

        async def handler(args):
            return {"done": True}

        executor = ToolExecutor()
        executor.register_handler(
            "p97_pool_wire", handler, spec=ToolSpec(name="p97_pool_wire", description="d")
        )
        result = await executor.execute("p97_pool_wire", {})
        assert result.success is True
        assert seen["pool"] == "neo4j"


class TestSharedHttpClient:
    @pytest.mark.asyncio
    async def test_disabled_yields_fresh_clients(self, monkeypatch):
        from agentix.infrastructure.http import acquire_http_client

        monkeypatch.setattr(settings, "SHARED_HTTP_MAX_CONNECTIONS", 0)
        async with acquire_http_client(timeout=5.0) as (c1, owns1):
            assert owns1 is True
        async with acquire_http_client(timeout=5.0) as (c2, owns2):
            assert owns2 is True
        assert c1 is not c2  # 0 = 每次新建（基线）
        assert c1.is_closed and c2.is_closed  # owns=True 由 CM 关闭

    @pytest.mark.asyncio
    async def test_enabled_reuses_single_shared_client(self, monkeypatch):
        from agentix.infrastructure.http import acquire_http_client, get_shared_async_client

        monkeypatch.setattr(settings, "SHARED_HTTP_MAX_CONNECTIONS", 4)
        async with acquire_http_client(timeout=5.0) as (c1, owns1):
            assert owns1 is False
        async with acquire_http_client(timeout=5.0) as (c2, owns2):
            assert owns2 is False
        assert c1 is c2
        assert not c1.is_closed  # 共享实例不被 CM 关闭
        assert get_shared_async_client() is c1

    @pytest.mark.asyncio
    async def test_aclose_and_rebuild(self, monkeypatch):
        from agentix.infrastructure.http import (
            aclose_shared_async_client,
            get_shared_async_client,
        )

        monkeypatch.setattr(settings, "SHARED_HTTP_MAX_CONNECTIONS", 4)
        first = get_shared_async_client()
        assert first is not None
        await aclose_shared_async_client()
        assert first.is_closed
        second = get_shared_async_client()
        assert second is not None and second is not first
        await aclose_shared_async_client()


# ===========================================================================
# 5. 降级阶梯 + 负载甩脱
# ===========================================================================


class TestLoadShedder:
    def test_classify_lag_levels(self, monkeypatch):
        from agentix.performance.load_shedder import ShedLevel, classify_load

        _no_downstream_impairment(monkeypatch)
        assert classify_load(lag_s=0.05) == ShedLevel.L0
        assert classify_load(lag_s=0.12) == ShedLevel.L1
        assert classify_load(lag_s=0.30) == ShedLevel.L2
        assert classify_load(lag_s=0.60) == ShedLevel.L3
        assert classify_load(lag_s=1.50) == ShedLevel.L4
        assert classify_load(lag_s=2.50) == ShedLevel.L5
        assert classify_load(lag_s=6.00) == ShedLevel.L6

    def test_classify_queue_and_downstream(self, monkeypatch):
        from agentix.performance.load_shedder import ShedLevel, classify_load

        _no_downstream_impairment(monkeypatch)
        # 队列比例分级（capacity=8）
        assert classify_load(queue_depth=1, queue_capacity=8) == ShedLevel.L1
        assert classify_load(queue_depth=4, queue_capacity=8) == ShedLevel.L2
        assert classify_load(queue_depth=6, queue_capacity=8) == ShedLevel.L3
        assert classify_load(queue_depth=8, queue_capacity=8) == ShedLevel.L4  # 满但 lag 低
        # 队列满 + lag≥1s → L6（礼貌拒绝）
        assert classify_load(queue_depth=8, queue_capacity=8, lag_s=1.2) == ShedLevel.L6
        # 下游熔断：1 个受损 → L2，≥2 个 → L4
        assert classify_load(impaired_downstreams=1) == ShedLevel.L2
        assert classify_load(impaired_downstreams=2) == ShedLevel.L4

    @pytest.mark.asyncio
    async def test_flag_off_stays_l0_and_never_sheds(self, monkeypatch):
        from agentix.observability import metrics
        from agentix.performance.load_shedder import ShedLevel, get_load_shedder

        monkeypatch.setattr(settings, "LOAD_SHEDDING_ENABLED", False)
        metrics.record_event_loop_lag(9.0)  # 高压输入但 flag 关
        shedder = get_load_shedder()
        assert shedder.current_level() == ShedLevel.L0
        assert shedder.should_shed("evolution_sleep") is False
        assert shedder.should_shed("file_import") is False

    @pytest.mark.asyncio
    async def test_sheds_background_by_priority(self, monkeypatch):
        from agentix.observability import metrics
        from agentix.performance.load_shedder import ShedLevel, get_load_shedder

        monkeypatch.setattr(settings, "LOAD_SHEDDING_ENABLED", True)
        _no_downstream_impairment(monkeypatch)
        shedder = get_load_shedder()

        # L1：只甩 evolution（最先）
        metrics.record_event_loop_lag(0.15)
        assert shedder.current_level() == ShedLevel.L1
        assert shedder.should_shed("evolution_sleep") is True
        assert shedder.should_shed("memory_consolidation") is False
        assert shedder.should_shed("file_import") is False
        assert shedder.should_shed("unknown_task") is False  # 未知类型保守不甩

        # L3：+ 记忆固化
        metrics.record_event_loop_lag(0.60)
        assert shedder.current_level() == ShedLevel.L3
        assert shedder.should_shed("memory_consolidation") is True
        assert shedder.should_shed("conversation_summary") is False

        # L4：+ 会话摘要
        metrics.record_event_loop_lag(1.50)
        assert shedder.current_level() == ShedLevel.L4
        assert shedder.should_shed("conversation_summary") is True
        assert shedder.should_shed("file_import") is False

        # L5：全后台（含文件导入）；L6 不在消费侧甩（交给准入礼貌拒绝）
        metrics.record_event_loop_lag(2.50)
        assert shedder.current_level() == ShedLevel.L5
        assert shedder.should_shed("file_import") is True
        metrics.record_event_loop_lag(6.00)
        assert shedder.current_level() == ShedLevel.L6
        assert shedder.should_shed("evolution_sleep") is False  # L6 由 admission 拒绝

    @pytest.mark.asyncio
    async def test_level_rise_records_degraded_once(self, monkeypatch):
        from agentix.observability import metrics
        from agentix.performance.load_shedder import get_load_shedder

        monkeypatch.setattr(settings, "LOAD_SHEDDING_ENABLED", True)
        _no_downstream_impairment(monkeypatch)
        before = _sample("degraded_total", component="load_shed_l4")

        metrics.record_event_loop_lag(1.50)
        shedder = get_load_shedder()
        shedder.current_level()
        shedder.current_level()  # 重复读不再计数
        assert _sample("degraded_total", component="load_shed_l4") == before + 1

    @pytest.mark.asyncio
    async def test_l6_polite_reject_via_admission(self, monkeypatch):
        from agentix.observability import metrics
        from agentix.performance.admission import admission_wait
        from agentix.runtime.control_plane import AdmissionDenied

        monkeypatch.setattr(settings, "LOAD_SHEDDING_ENABLED", True)
        monkeypatch.setattr(settings, "ADMISSION_CONTROL_ENABLED", True)
        _no_downstream_impairment(monkeypatch)
        metrics.record_event_loop_lag(6.0)  # L6，但并发未到顶

        with pytest.raises(AdmissionDenied, match="load shed L6"):
            await admission_wait(kind="test")


# ===========================================================================
# 6. task_queue 消费侧甩负荷
# ===========================================================================


class _FakeQueueRedis:
    def __init__(self, task):
        self._raw = json.dumps(task.to_dict())
        self.sets = []
        self.acked = []

    async def get(self, key):
        return self._raw

    async def set(self, key, value, **kw):
        self.sets.append((key, value))

    async def xack(self, *args):
        self.acked.append(args)


class TestTaskQueueShed:
    @pytest.mark.asyncio
    async def test_sheds_background_task_without_running_handler(self, monkeypatch):
        from agentix.application.services.task_queue import Task, TaskQueue
        from agentix.observability import metrics
        from agentix.performance.load_shedder import get_load_shedder

        monkeypatch.setattr(settings, "LOAD_SHEDDING_ENABLED", True)
        _no_downstream_impairment(monkeypatch)
        metrics.record_event_loop_lag(0.2)  # L1 → evolution 首甩

        task = Task(task_id="p97t1", task_type="evolution_sleep", payload={})
        redis = _FakeQueueRedis(task)
        queue = TaskQueue(redis_client=redis)
        ran = False

        @queue.register("evolution_sleep")
        async def _handler(payload):
            nonlocal ran
            ran = True
            return {}

        await queue._process_message(
            "1-0",
            {"task_id": "p97t1", "task_type": "evolution_sleep", "payload": "{}"},
        )
        assert ran is False  # handler 未执行（负荷已甩）
        assert any("load shed" in (v or "") for _, v in redis.sets) or task.error
        assert get_load_shedder() is not None

    @pytest.mark.asyncio
    async def test_flag_off_runs_handler(self, monkeypatch):
        from agentix.application.services.task_queue import Task, TaskQueue
        from agentix.observability import metrics

        monkeypatch.setattr(settings, "LOAD_SHEDDING_ENABLED", False)
        metrics.record_event_loop_lag(9.0)  # 高压但 flag 关

        task = Task(task_id="p97t2", task_type="evolution_sleep", payload={})
        redis = _FakeQueueRedis(task)
        queue = TaskQueue(redis_client=redis)
        ran = False

        @queue.register("evolution_sleep")
        async def _handler(payload):
            nonlocal ran
            ran = True
            return {}

        await queue._process_message(
            "1-0",
            {"task_id": "p97t2", "task_type": "evolution_sleep", "payload": "{}"},
        )
        assert ran is True


# ===========================================================================
# 7. 五处同步守卫（p41 主检 configuration.md/p41 清单，这里补 selfcheck/.env）
# ===========================================================================


class TestW3FlagSync:
    W3_KEYS = (
        "ADMISSION_CONTROL_ENABLED",
        "ADMISSION_MAX_CONCURRENT_RUNS",
        "ADMISSION_MAX_QUEUE",
        "RETRY_BUDGET_ENABLED",
        "RETRY_BUDGET_TOKENS",
        "ADAPTIVE_TOOL_TIMEOUT_ENABLED",
        "LOAD_SHEDDING_ENABLED",
        "BULKHEAD_RETRIEVAL_SIZE",
        "BULKHEAD_NEO4J_SIZE",
        "BULKHEAD_MCP_SIZE",
        "BULKHEAD_WEB_SEARCH_SIZE",
        "SHARED_HTTP_MAX_CONNECTIONS",
    )

    def test_defaults_are_off(self):
        assert settings.ADMISSION_CONTROL_ENABLED is False
        assert settings.RETRY_BUDGET_ENABLED is False
        assert settings.ADAPTIVE_TOOL_TIMEOUT_ENABLED is False
        assert settings.LOAD_SHEDDING_ENABLED is False
        assert settings.ADMISSION_MAX_CONCURRENT_RUNS == 0
        assert settings.ADMISSION_MAX_QUEUE == 0
        assert settings.RETRY_BUDGET_TOKENS == 6
        assert settings.BULKHEAD_RETRIEVAL_SIZE == 0
        assert settings.BULKHEAD_NEO4J_SIZE == 0
        assert settings.BULKHEAD_MCP_SIZE == 0
        assert settings.BULKHEAD_WEB_SEARCH_SIZE == 0
        assert settings.SHARED_HTTP_MAX_CONNECTIONS == 0

    def test_selfcheck_allows_omission(self):
        from agentix.config.selfcheck import DEFAULT_IGNORED_MISSING

        for key in self.W3_KEYS:
            assert key in DEFAULT_IGNORED_MISSING, f"{key} 不在 selfcheck 白名单"

    def test_env_example_documents_all_keys(self):
        env_text = Path(".env.example").read_text(encoding="utf-8")
        for key in self.W3_KEYS:
            assert key in env_text, f"{key} 不在 .env.example"
