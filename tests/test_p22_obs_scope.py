"""P2-2 验收测试：可观测性接线 + application→interfaces 反向依赖棘轮。

四项交付：
1. ``trace_id`` 下沉 domain（``agentix.domain.tracing``）；application 层
   interfaces 反向导入收敛为**棘轮白名单**（只允许减少，不允许新增；middleware /
   v1 / models.chat 零豁免）
2. 日志按请求可关联：``DEFAULT_FORMAT`` 携带 ``{extra[trace_id]}`` /
   ``{extra[service]}``，patcher 在**每条记录发出时**读取上下文（不串味）
3. 请求级 token / tool / cost 聚合（``request_scope``，含流式终态落库），并证明
   ``asyncio.gather`` 子任务的累加对父作用域**可见**
4. ``status`` 收敛为 ``RequestStatus`` 枚举

全部离线：mock graph / 内存缓冲区；仅部分用例 mock 掉 observability 以免连 Redis。
"""

import ast
import asyncio
import io
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage

from agentix.application.services.chat_service import ChatService
from agentix.application.services.request_scope import (
    get_current_scope,
    record_llm_usage,
    record_tool,
    request_scope,
)
from agentix.config import settings as settings_instance
from agentix.domain.errors import AgentError, ErrorCode
from agentix.domain.status import RequestStatus
from agentix.domain.tracing import get_trace_id, trace_id_var

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APPLICATION_DIR = PROJECT_ROOT / "agentix" / "application"


# ---------------------------------------------------------------------------
# Item 1：分层修复 + AST 守卫
# ---------------------------------------------------------------------------


def _iter_py_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        yield path


def _imported_module_names(path: Path) -> List[str]:
    """AST 提取所有 ``import`` / ``from ... import`` 的目标模块名。

    用 AST 而非源码子串匹配：注释/字符串里出现 ``agentix.interfaces`` 不算命中。
    """
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    modules: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # 相对导入（level>0）不可能是对绝对包路径 agentix.interfaces 的引用
            if node.level == 0 and node.module:
                modules.append(node.module)
    return modules


# ---------------------------------------------------------------------------
# Item 1 棘轮守卫的冻结白名单（frozenset）
#
# P2-6 (T5) 已收紧为**空集**：application → interfaces 的任何导入都零容忍。
# 历史（批次 6 冻结期）：chat_service 曾反向依赖 interfaces 的 HTTP 契约
# schema（ChatMessageCreate / ChatSessionCreate），P2-6 T1-T4 将 CRUD 载体
# 下沉为 domain.ports.payloads 的 TypedDict 后，这两条已消除。守卫用 ``==``
# 断言空集：未来任何 application→interfaces 导入（含新增或复活旧路径）都会
# 让守卫失败。
# ---------------------------------------------------------------------------
KNOWN_INTERFACES_IMPORTS = frozenset()

INTERFACES_PREFIX = "agentix.interfaces"

# 零豁免目标：这些 interfaces 目标在 application 下必须为 0，且**不得**进白名单。
ZERO_TOLERANCE_TARGETS = (
    "agentix.interfaces.http.middleware",
    "agentix.interfaces.http.v1",
    "agentix.interfaces.http.models.chat",
)


def _hits_zero_tolerance(module: str) -> bool:
    return any(
        module == z or module.startswith(z + ".") for z in ZERO_TOLERANCE_TARGETS
    )


def _interfaces_imports(root: Path):
    """收集 application 下所有指向 ``agentix.interfaces.*`` 的导入三元组。

    返回 ``(相对路径, 模块, 名称)`` 列表；``from X import a, b`` 展开为
    ``(rel, X, a)`` / ``(rel, X, b)``；``import X`` 变为 ``(rel, X, X)``。
    """
    found = []
    for path in _iter_py_files(root):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level != 0 or not node.module:
                    continue
                if node.module == INTERFACES_PREFIX or node.module.startswith(
                    INTERFACES_PREFIX + "."
                ):
                    found.extend((rel, node.module, alias.name) for alias in node.names)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == INTERFACES_PREFIX or alias.name.startswith(
                        INTERFACES_PREFIX + "."
                    ):
                        found.append((rel, alias.name, alias.name))
    return sorted(found)


class TestTracingLayerFix:
    def test_domain_tracing_is_pure_stdlib(self):
        """domain 层不得引入第三方依赖（tests/test_p17 规则）。"""
        modules = _imported_module_names(
            PROJECT_ROOT / "agentix" / "domain" / "tracing.py"
        )
        roots = {m.split(".")[0] for m in modules}
        assert roots <= {"contextvars"}, f"tracing.py 非 stdlib 依赖: {roots}"

    def test_middleware_reexports_same_contextvar_object(self):
        """旧路径 ``interfaces.http.middleware.trace_id_var`` 必须是同一对象。"""
        from agentix.interfaces.http.middleware import trace_id_var as legacy_var

        assert legacy_var is trace_id_var

    def test_get_trace_id_reads_contextvar(self):
        trace_id_var.set("tid-abc")
        assert get_trace_id() == "tid-abc"
        trace_id_var.set("-")

    def test_trace_id_middleware_behaviour_unchanged(self):
        """fastapi TestClient 走一遍：生成/透传/回写响应头仍正确。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from agentix.interfaces.http.middleware import TraceIdMiddleware

        app = FastAPI()
        app.add_middleware(TraceIdMiddleware)

        @app.get("/t")
        async def t():
            from agentix.domain.tracing import get_trace_id as g

            return {"tid": g()}

        client = TestClient(app)
        resp = client.get("/t", headers={"X-Trace-Id": "hdr-1"})
        assert resp.headers["X-Trace-Id"] == "hdr-1"
        assert resp.json()["tid"] == "hdr-1"

    def test_application_interfaces_imports_match_frozen_allowlist(self):
        """棘轮守卫：application→interfaces 导入集合 **恰好等于** 冻结白名单。

        用 ``==``（而非 ``issubset``）：多一条挡新违例，少一条逼人回来收紧白名单。
        """
        found = set(_interfaces_imports(APPLICATION_DIR))
        assert found == set(
            KNOWN_INTERFACES_IMPORTS
        ), f"application→interfaces 反向依赖发生变化（只允许减少）：实际={sorted(found)}"

    def test_zero_tolerance_targets_are_not_imported_at_all(self):
        """零豁免：middleware / v1 / models.chat 在 application 下必须为 0。"""
        bad = [
            t
            for t in _interfaces_imports(APPLICATION_DIR)
            if _hits_zero_tolerance(t[1])
        ]
        assert bad == [], f"这些 interfaces 目标必须零引用（不得豁免）：{bad}"

    def test_allowlist_does_not_smuggle_zero_tolerance_targets(self):
        """白名单本身不得包含零豁免目标。"""
        bad = sorted(
            entry for entry in KNOWN_INTERFACES_IMPORTS if _hits_zero_tolerance(entry[1])
        )
        assert bad == [], f"白名单混入零豁免目标：{bad}"

    def test_chat_service_has_known_issue_annotation(self):
        """被冻结的两条反向导入上方必须有可 grep 的 KNOWN-ISSUE(P2-6) 标注。"""
        src = (
            APPLICATION_DIR / "services" / "chat_service.py"
        ).read_text(encoding="utf-8")
        assert "KNOWN-ISSUE(P2-6)" in src

    def test_chat_service_imports_domain_tracing_not_middleware(self):
        src = (
            APPLICATION_DIR / "services" / "chat_service.py"
        ).read_text(encoding="utf-8")
        assert "from agentix.domain.tracing import get_trace_id" in src
        assert "agentix.interfaces.http.middleware" not in src

    def test_llm_factory_imports_domain_tracing(self):
        src = (
            APPLICATION_DIR / "agents" / "llm_factory.py"
        ).read_text(encoding="utf-8")
        assert "from agentix.domain.tracing import get_trace_id" in src
        assert "agentix.interfaces" not in src


# ---------------------------------------------------------------------------
# Item 2：日志 trace_id 关联
# ---------------------------------------------------------------------------


class TestLogCorrelation:
    def test_default_format_has_trace_and_service_placeholders(self):
        from agentix.infrastructure.core.logger import DEFAULT_FORMAT

        assert "{extra[trace_id]}" in DEFAULT_FORMAT
        assert "{extra[service]}" in DEFAULT_FORMAT

    def test_configure_logging_wires_global_patcher(self, monkeypatch):
        """``configure_logging`` 必须设置 patcher（loguru 经 ``logger.configure``）。"""
        import agentix.infrastructure.core.logger as logmod

        captured: Dict[str, Any] = {}

        def fake_configure(**kwargs):
            captured.update(kwargs)

        def fake_add(sink, **kwargs):
            captured["add"] = kwargs
            return 0

        monkeypatch.setattr(logmod.logger, "remove", lambda *a, **k: None)
        monkeypatch.setattr(logmod.logger, "configure", fake_configure)
        monkeypatch.setattr(logmod.logger, "add", fake_add)

        logmod.configure_logging(level="INFO")

        assert captured.get("patcher") is logmod._inject_context
        assert "{extra[trace_id]}" in captured["add"]["format"]
        assert "{extra[service]}" in captured["add"]["format"]

    def test_patcher_reads_context_at_emit_time_no_cross_contamination(self):
        """核心断言：两条记录的 trace_id 各自独立，互不串味。"""
        import agentix.infrastructure.core.logger as logmod
        from loguru import logger as loguru_logger

        buf = io.StringIO()
        loguru_logger.configure(patcher=logmod._inject_context)
        hid = loguru_logger.add(
            buf, format=logmod.DEFAULT_FORMAT, level="DEBUG", colorize=False
        )
        try:
            log = logmod.get_logger(service="svc-x")
            trace_id_var.set("t-A")
            log.info("alpha")
            trace_id_var.set("t-B")
            log.info("bravo")
        finally:
            loguru_logger.remove(hid)
            loguru_logger.configure(patcher=None)
            trace_id_var.set("-")

        lines = [ln for ln in buf.getvalue().splitlines() if "alpha" in ln or "bravo" in ln]
        assert len(lines) == 2
        assert "t-A" in lines[0] and "t-B" not in lines[0]
        assert "t-B" in lines[1] and "t-A" not in lines[1]
        assert "svc-x" in lines[0] and "svc-x" in lines[1]

    def test_patcher_defaults_service_when_unbound(self):
        from agentix.infrastructure.core.logger import _inject_context

        record = {"extra": {}}
        trace_id_var.set("t-Z")
        try:
            _inject_context(record)
        finally:
            trace_id_var.set("-")
        assert record["extra"]["trace_id"] == "t-Z"
        assert record["extra"]["service"] == "-"


# ---------------------------------------------------------------------------
# Item 3：请求级作用域
# ---------------------------------------------------------------------------


class TestRequestScope:
    def test_no_active_scope_is_silent_noop(self):
        assert get_current_scope() is None
        # 不抛异常即为通过
        record_llm_usage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        record_tool("nope")
        assert get_current_scope() is None

    @pytest.mark.asyncio
    async def test_records_usage_and_tools(self):
        async with request_scope() as scope:
            record_llm_usage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
            record_llm_usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
            record_tool("milvus_search")
            record_tool("milvus_search")  # 去重
            record_tool("postgres_search")
        assert scope.llm_calls == 2
        assert scope.prompt_tokens == 110
        assert scope.completion_tokens == 55
        assert scope.total_tokens == 165
        assert scope.tools == ["milvus_search", "postgres_search"]

    @pytest.mark.asyncio
    async def test_total_tokens_falls_back_to_prompt_plus_completion(self):
        async with request_scope() as scope:
            record_llm_usage(prompt_tokens=7, completion_tokens=3)  # total 缺省为 0
        assert scope.total_tokens == 10

    @pytest.mark.asyncio
    async def test_estimated_cost_uses_prices(self):
        async with request_scope() as scope:
            record_llm_usage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500)
            cost = scope.estimated_cost(
                input_price_per_1k=0.001, output_price_per_1k=0.002
            )
        assert cost == pytest.approx(0.001 * 1 + 0.002 * 0.5)

    @pytest.mark.asyncio
    async def test_scope_resets_on_exit(self):
        async with request_scope() as scope:
            assert get_current_scope() is scope
        assert get_current_scope() is None
        # 退出后再记录是 no-op（不写到上一个 scope）
        record_llm_usage(total_tokens=99)
        assert scope.total_tokens == 0


class TestGatherVisibility:
    """Item 3 关键：证明 gather 子任务的累加对父作用域可见。

    ``asyncio.gather`` 会把当前 context **浅拷贝**给子任务。本实现让
    ContextVar 持有**一个可变对象**的引用——子任务拷贝的是同一个引用，故
    子任务内的 increment 对父作用域立即可见。若改用 ``ContextVar[int]``，
    子任务只会写自己的 context 副本，父作用域看不到（会丢数据）。
    """

    @pytest.mark.asyncio
    async def test_gathered_task_increments_are_visible_to_parent(self):
        async def worker(n: int) -> None:
            await asyncio.sleep(0)  # 让出控制权，确保真正并发调度
            record_llm_usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
            record_tool(f"tool-{n}")

        async with request_scope() as scope:
            await asyncio.gather(worker(1), worker(2), worker(3))

            # 核心断言：父作用域看到了全部三个子任务的累加
            assert scope.llm_calls == 3
            assert scope.total_tokens == 45
            assert sorted(scope.tools) == ["tool-1", "tool-2", "tool-3"]


# ---------------------------------------------------------------------------
# Item 3：chat_service 接线（scope → RequestMetrics）
# ---------------------------------------------------------------------------


class _FakeGraph:
    """可控 mock 图：可选地模拟 LLM/tool 用量，再返回结果或抛异常。"""

    def __init__(
        self,
        result: Optional[Dict[str, Any]] = None,
        exc: Optional[BaseException] = None,
        usage: Optional[Dict[str, int]] = None,
        tools: Optional[List[str]] = None,
    ) -> None:
        self._result = result
        self._exc = exc
        self._usage = usage
        self._tools = tools or []

    async def ainvoke(self, input_state, config=None):
        for t in self._tools:
            record_tool(t)
        if self._usage is not None:
            record_llm_usage(**self._usage)
        if self._exc is not None:
            raise self._exc
        return self._result


def _ok_result(messages: Optional[List[Any]] = None) -> Dict[str, Any]:
    return {
        "messages": messages or [AIMessage(content="答复")],
        "router": {"type": "legal", "logic": "test"},
        "sources": [],
    }


@pytest.fixture
def _obs_mock(monkeypatch):
    import agentix.application.services.observability as obs_module

    svc = AsyncMock()
    svc.record_request = AsyncMock()
    monkeypatch.setattr(obs_module, "get_observability", lambda: svc)
    return svc


class TestAskFillsRequestMetrics:
    @pytest.mark.asyncio
    async def test_success_fills_scope_numbers(self, monkeypatch, _obs_mock):
        graph = _FakeGraph(
            result=_ok_result(),
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            tools=["milvus_search"],
        )
        svc = ChatService(graph_getter=lambda: graph)

        result = await svc.ask("q", "s1")

        assert result["status"] == RequestStatus.OK.value
        _obs_mock.record_request.assert_awaited_once()
        metrics = _obs_mock.record_request.await_args.args[0]
        assert metrics.success is True
        assert metrics.llm_calls == 1
        assert metrics.prompt_tokens == 100
        assert metrics.completion_tokens == 50
        assert metrics.total_tokens == 150
        assert metrics.tools_used == ["milvus_search"]

    @pytest.mark.asyncio
    async def test_estimated_cost_uses_settings_prices(self, monkeypatch, _obs_mock):
        monkeypatch.setattr(settings_instance, "LLM_INPUT_PRICE_PER_1K", 0.001)
        monkeypatch.setattr(settings_instance, "LLM_OUTPUT_PRICE_PER_1K", 0.002)
        graph = _FakeGraph(
            result=_ok_result(),
            usage={"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
        )
        svc = ChatService(graph_getter=lambda: graph)

        await svc.ask("q", "s1")

        metrics = _obs_mock.record_request.await_args.args[0]
        assert metrics.estimated_cost == pytest.approx(0.001 + 0.001)

    @pytest.mark.asyncio
    async def test_default_price_is_zero(self):
        """本地 LM Studio 免费 → 默认估算成本必须是诚实的 0.0。"""
        assert settings_instance.LLM_INPUT_PRICE_PER_1K == 0.0
        assert settings_instance.LLM_OUTPUT_PRICE_PER_1K == 0.0

    @pytest.mark.asyncio
    async def test_degradable_error_path_still_fills_scope(self, monkeypatch, _obs_mock):
        graph = _FakeGraph(
            exc=AgentError(ErrorCode.RETRIEVAL_FAILED, "retrieval down"),
            usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        )
        svc = ChatService(graph_getter=lambda: graph)

        result = await svc.ask("q", "s1")

        assert result["status"] == RequestStatus.DEGRADED.value
        _obs_mock.record_request.assert_awaited_once()
        metrics = _obs_mock.record_request.await_args.args[0]
        assert metrics.success is False
        assert metrics.error == "retrieval_failed"
        assert metrics.llm_calls == 1
        assert metrics.total_tokens == 30

    @pytest.mark.asyncio
    async def test_non_streaming_tools_extracted_from_result_messages(
        self, monkeypatch, _obs_mock
    ):
        """非流式路径看不到事件，从结果消息的 tool_calls 里 best-effort 提取工具名。"""
        msg = AIMessage(
            content="",
            tool_calls=[
                {"name": "milvus_search", "args": {}, "id": "1"},
                {"name": "postgres_search", "args": {}, "id": "2"},
            ],
        )
        graph = _FakeGraph(result=_ok_result(messages=[msg, AIMessage(content="done")]))
        svc = ChatService(graph_getter=lambda: graph)

        await svc.ask("q", "s1")

        metrics = _obs_mock.record_request.await_args.args[0]
        assert metrics.tools_used == ["milvus_search", "postgres_search"]

    @pytest.mark.asyncio
    async def test_scope_does_not_leak_between_requests(self, monkeypatch, _obs_mock):
        graph = _FakeGraph(
            result=_ok_result(),
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )
        svc = ChatService(graph_getter=lambda: graph)

        await svc.ask("q1", "s1")
        await svc.ask("q2", "s1")

        first, second = [
            c.args[0] for c in _obs_mock.record_request.await_args_list
        ]
        # 每次请求各自的 scope：第二次不是第一次的累加
        assert first.llm_calls == 1 and second.llm_calls == 1
        assert first.total_tokens == 15 and second.total_tokens == 15
        assert get_current_scope() is None


# ---------------------------------------------------------------------------
# Item 3：stream_events 接线
# ---------------------------------------------------------------------------


class _FakeStreamGraph:
    """最小 astream_events 替身；进入迭代时记录 scope 是否活跃。

    可选 ``usage`` / ``tools``：在迭代开始处模拟真实 LLM/工具用量。
    """

    def __init__(
        self,
        events: List[Dict[str, Any]],
        usage: Optional[Dict[str, int]] = None,
        tools: Optional[List[str]] = None,
    ) -> None:
        self._events = events
        self._usage = usage
        self._tools = tools or []
        self.scope_active_during_stream = None

    async def astream_events(self, input_state, config=None, version=None):
        self.scope_active_during_stream = get_current_scope() is not None
        for t in self._tools:
            record_tool(t)
        if self._usage is not None:
            record_llm_usage(**self._usage)
        for event in self._events:
            yield event


class _RaisingStreamGraph:
    """产出若干事件后抛异常（触发 impl 的 error 终态路径）。"""

    async def astream_events(self, input_state, config=None, version=None):
        yield _ev("on_tool_start", name="milvus_search")
        raise RuntimeError("boom")


def _ev(kind, name=None, data=None, metadata=None):
    return {"event": kind, "name": name, "data": data or {}, "metadata": metadata or {}}


def _collect(graph, message="hi", session_id="s1"):
    async def _run():
        svc = ChatService(graph_getter=lambda: graph)
        out = []
        async for e in svc.stream_events(message, session_id):
            out.append(e)
        return out

    return asyncio.run(_run())


class TestStreamEventsScope:
    def test_stream_runs_inside_request_scope(self):
        graph = _FakeStreamGraph(
            [
                _ev("on_chain_end", name="LangGraph", data={"output": {}}),
            ]
        )
        _collect(graph)
        assert graph.scope_active_during_stream is True
        # 流结束后作用域被 reset，不残留到下一请求
        assert get_current_scope() is None

    def test_on_tool_start_records_tool(self, monkeypatch):
        import agentix.application.services.chat_service as cs

        recorded: List[str] = []
        monkeypatch.setattr(cs, "record_tool", lambda name: recorded.append(name))

        graph = _FakeStreamGraph(
            [
                _ev("on_tool_start", name="milvus_search"),
                _ev("on_tool_start", name="postgres_search"),
                _ev("on_chain_end", name="LangGraph", data={"output": {}}),
            ]
        )
        _collect(graph)
        assert recorded == ["milvus_search", "postgres_search"]


class TestStreamEventsRecordRequest:
    """P2-2 补充：流式在**首个终态帧**落库恰好一次，SSE 帧不变。"""

    def test_records_once_on_done_with_scope_numbers(self, _obs_mock):
        graph = _FakeStreamGraph(
            [
                _ev(
                    "on_chain_end",
                    name="analyze_and_route_query",
                    data={"output": {"router": {"type": "legal", "logic": "kb"}}},
                    metadata={"langgraph_node": "analyze_and_route_query"},
                ),
                _ev("on_chain_end", name="LangGraph", data={"output": {}}),
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            tools=["milvus_search"],
        )
        events = _collect(graph)

        # SSE 帧内容/顺序零改动（首个终态仍是 done，且只有一次）
        assert [e["type"] for e in events][-1] == "done"
        assert [e["type"] for e in events].count("done") == 1

        _obs_mock.record_request.assert_awaited_once()
        m = _obs_mock.record_request.await_args.args[0]
        assert m.success is True
        assert m.error is None
        assert m.route_type == "legal"
        assert m.llm_calls == 1
        assert m.total_tokens == 15
        assert m.tools_used == ["milvus_search"]

    def test_records_once_on_error_path(self, _obs_mock):
        events = _collect(_RaisingStreamGraph())

        assert "error" in [e["type"] for e in events]
        _obs_mock.record_request.assert_awaited_once()
        m = _obs_mock.record_request.await_args.args[0]
        assert m.success is False
        assert m.error == "stream_error"


# ---------------------------------------------------------------------------
# Item 4：status 枚举
# ---------------------------------------------------------------------------


class TestRequestStatus:
    def test_exact_value_set(self):
        assert {s.value for s in RequestStatus} == {"ok", "degraded", "error"}

    def test_str_enum_stays_plain_string(self):
        assert RequestStatus.OK == "ok"
        assert RequestStatus.DEGRADED == "degraded"
        assert RequestStatus.ERROR == "error"

    def test_domain_status_is_pure_stdlib(self):
        # M0（统一执行链 Stage M）：ExitStatus/ExitReason 规范位已迁
        # domain/runtime/lifecycle.py——纯度守卫随契约迁至新家；M6 清尾后
        # status.py 只剩 RequestStatus（不再 re-export lifecycle 枚举）。
        status_roots = {
            m.split(".")[0]
            for m in _imported_module_names(PROJECT_ROOT / "agentix" / "domain" / "status.py")
        }
        assert status_roots <= {"enum"}, f"status.py 非纯依赖: {status_roots}"
        lifecycle_roots = {
            m.split(".")[0]
            for m in _imported_module_names(
                PROJECT_ROOT / "agentix" / "domain" / "runtime" / "lifecycle.py"
            )
        }
        assert lifecycle_roots <= {"enum"}, f"lifecycle.py 非 stdlib 依赖: {lifecycle_roots}"

    def test_status_module_no_longer_reexports_lifecycle(self):
        """M6 清尾：status.py 不再 re-export lifecycle 枚举，调用方直连规范位。"""
        import agentix.domain.runtime.lifecycle as canon
        import agentix.domain.status as mod

        assert not hasattr(mod, "ExitStatus")
        assert not hasattr(mod, "ExitReason")
        assert canon.ExitStatus.COMPLETED == "completed"

    def test_chat_service_uses_enum_not_bare_literals(self):
        src = (
            APPLICATION_DIR / "services" / "chat_service.py"
        ).read_text(encoding="utf-8")
        assert "RequestStatus" in src
        # 请求 status 字段不再写裸字符串字面量
        assert '"status": "ok"' not in src
        assert '"status": "degraded"' not in src
        assert '"status": "error"' not in src
