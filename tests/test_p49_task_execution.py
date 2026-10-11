"""P1（岗位对照·任务执行）：任务型路由 + 写工具 + 任务管线 + 重规划接线。

把 P1 的 DoD 逐条钉死：

1. **flag 治理**：``TASK_EXEC_ENABLED`` / ``AGENT_MAX_REPLANS`` 默认 off/0，
   flag-off 时路由提示词与基线**字节等价**、白名单不放行 task-query、
   两个写工具零注册、任务管线首行即返 ``task_exec_disabled``；
2. **路由**：flag-on 提示词追加 ``TASK_EXEC_ROUTE_BLOCK`` + 结构化输出换
   ``TaskRouter`` schema；``route_query`` 把 ``task-query`` 派发到
   ``execute_task``（节点/ Literal/ ``ROUTE_HANDLERS`` 三面一致）；
3. **写工具治理链路**：声明（flag 门控）→ 风险（L1→MEDIUM / write→HIGH）
   → HITL 闸（gate-on 时 ``review_required``，handler 绝不预跑）→
   落 ``InMemoryArtifactStore``（roundtrip + ``rebuildable=False``）；
4. **任务管线四终态**：completed / failed（拆解失败）/ budget_exceeded /
   verify_failed（全失败、HITL 拦截），波次并行与预算前置检查；
5. **节点与重规划**：``execute_task`` flag-off no-op / flag-on 回写
   ``TaskRouter`` + 逐步 tool_calls；``AGENT_MAX_REPLANS>0`` 时
   ``run_agent_loop_ask`` 真实触发重规划。

全局单例（声明缓存 / executor / artifact store / review gate）在夹具里
成对复位，与 Phase 21/23 测试同款范式。
"""

from __future__ import annotations

import pytest

from langchain_core.messages import HumanMessage

import agentix.application.agents.router as router_mod
import agentix.application.services.task_execution as task_execution_mod
from agentix.application.agents.lg_prompts import (
    ROUTER_SYSTEM_PROMPT,
    TASK_EXEC_ROUTE_BLOCK,
)
from agentix.orchestration.langgraph.lg_states import AgentState, Router, TaskRouter
from agentix.application.agents.router import route_query
from agentix.application.agents.task_handler import execute_task
from agentix.application.services.agent_loop_adapter import run_agent_loop_ask
from agentix.application.services.artifact_store import (
    get_artifact_store,
    reset_artifact_store,
)
from agentix.application.services.task_execution import (
    run_task_execution,
)
from agentix.application.services.task_tools_bridge import install_task_write_tools
from agentix.tools import registry_init
from agentix.tools.definitions import ToolSpec, registry as tool_registry
from agentix.tools.gateway.executor import (
    get_tool_executor,
    reset_tool_executor,
)
from agentix.tools.gateway.human_review import (
    reset_review_gate,
)
from agentix.config.settings import settings

# ---------------------------------------------------------------------------
# 全局单例清理 + flag 夹具（与 test_agent_phase23_interaction 同款范式）
# ---------------------------------------------------------------------------

_TASK_TOOLS = ("append_task_note", "write_task_artifact", "read_artifact")


def _reset_task_decls() -> None:
    """清声明缓存 + 摘掉 flag-on 时挂进全局 registry 的任务写工具。"""
    registry_init._DECLARATIONS.clear()
    for name in _TASK_TOOLS:
        tool_registry.delete_tool(name)


@pytest.fixture
def task_on(monkeypatch: pytest.MonkeyPatch):
    """flag-on：声明缓存 / executor / artifact store / review gate 四单例复位。"""
    monkeypatch.setattr(settings, "TASK_EXEC_ENABLED", True)
    _reset_task_decls()
    reset_tool_executor()
    reset_artifact_store()
    reset_review_gate()
    install_task_write_tools()
    yield
    _reset_task_decls()
    reset_tool_executor()
    reset_artifact_store()
    reset_review_gate()


@pytest.fixture
def task_off(monkeypatch: pytest.MonkeyPatch):
    """flag-off：与基线一致的零注册状态。"""
    monkeypatch.setattr(settings, "TASK_EXEC_ENABLED", False)
    _reset_task_decls()
    reset_tool_executor()
    reset_artifact_store()
    yield
    _reset_task_decls()
    reset_tool_executor()
    reset_artifact_store()
    reset_review_gate()


def _state(content: str = "你好", router: Router | None = None) -> AgentState:
    kwargs: dict = {"messages": [HumanMessage(content=content)]}
    if router is not None:
        kwargs["router"] = router
    return AgentState(**kwargs)


class _FakeRouterLLM:
    """替身 router LLM：捕获 schema/提示词，按捕获的 cls 构造响应。"""

    def __init__(self, response) -> None:
        self._response = response
        self.structured_cls = None
        self.seen_messages = None

    def with_structured_output(self, cls):
        self.structured_cls = cls
        return self

    async def ainvoke(self, messages):
        self.seen_messages = messages
        return self._response


# ---------------------------------------------------------------------------
# 1. flag 治理（默认 off；flag-off 字节等价 + 零注册）
# ---------------------------------------------------------------------------


class TestFlagGovernance:
    def test_default_flags_off(self):
        assert settings.TASK_EXEC_ENABLED is False
        assert settings.AGENT_MAX_REPLANS == 0

    @pytest.mark.asyncio
    async def test_flag_off_prompt_is_byte_equal_to_baseline(self, monkeypatch):
        fake = _FakeRouterLLM(response=Router(type="kb-query", logic="fake"))
        monkeypatch.setattr(router_mod, "get_llm_client", lambda **kw: fake)
        out = await router_mod.analyze_and_route_query(
            _state("你好"), config={}
        )
        assert fake.structured_cls is Router  # schema 未换成 TaskRouter
        assert fake.seen_messages[0]["content"] == ROUTER_SYSTEM_PROMPT
        # 反向断言：任务段确实存在但**未**被拼进基线提示
        assert "task-query" in TASK_EXEC_ROUTE_BLOCK
        assert "task-query" not in ROUTER_SYSTEM_PROMPT
        assert type(out["router"]) is Router

    @pytest.mark.asyncio
    async def test_flag_off_task_query_falls_back_to_kb(self):
        # TaskRouter 实例带着 task-query 进来（仅测试可构造），flag-off 必须降级
        state = _state(
            "记一条笔记", router=TaskRouter(type="task-query", logic="test")
        )
        assert route_query(state) == "create_kb_query"

    def test_flag_off_zero_registration(self, task_off):
        registry_init.ensure_registered()
        ex = get_tool_executor()
        for name in _TASK_TOOLS:
            assert not tool_registry.has_tool(name)
            assert not ex.has_handler(name)

    def test_default_executor_has_no_task_handlers(self):
        # 未经 install_task_write_tools 的执行器不带任务写工具
        ex = get_tool_executor()
        for name in _TASK_TOOLS:
            assert not ex.has_handler(name)


# ---------------------------------------------------------------------------
# 2. flag-on 路由（提示词段 / TaskRouter schema / execute_task 派发）
# ---------------------------------------------------------------------------


class TestRoutingFlagOn:
    @pytest.mark.asyncio
    async def test_flag_on_prompt_appends_task_block(self, monkeypatch):
        monkeypatch.setattr(settings, "TASK_EXEC_ENABLED", True)
        fake = _FakeRouterLLM(
            response=TaskRouter(type="task-query", logic="fake-task")
        )
        monkeypatch.setattr(router_mod, "get_llm_client", lambda **kw: fake)
        out = await router_mod.analyze_and_route_query(
            _state("帮我把要点记下来"), config={}
        )
        assert fake.structured_cls is TaskRouter  # 结构化输出 schema 已换
        assert fake.seen_messages[0]["content"] == (
            ROUTER_SYSTEM_PROMPT + TASK_EXEC_ROUTE_BLOCK
        )
        assert out["router"].type == "task-query"
        assert route_query(_state(router=out["router"])) == "execute_task"

    @pytest.mark.asyncio
    async def test_flag_on_dict_shaped_response_accepted(self, monkeypatch):
        # with_structured_output 偶发返回 dict 形状时同样要能校验成 TaskRouter
        monkeypatch.setattr(settings, "TASK_EXEC_ENABLED", True)
        fake = _FakeRouterLLM(
            response={"type": "task-query", "logic": "dict-shape"}
        )
        monkeypatch.setattr(router_mod, "get_llm_client", lambda **kw: fake)
        out = await router_mod.analyze_and_route_query(_state("记笔记"), config={})
        assert isinstance(out["router"], TaskRouter)
        assert out["router"].type == "task-query"

    def test_unknown_type_still_raises(self):
        # 上游污染出字面量外的 type（绕过校验的赋值）→ 与基线一致：显式 raise
        router = Router(type="kb-query")
        router.type = "nonsense-query"  # validate_assignment 默认关，可赋值
        with pytest.raises(ValueError, match="Unknown router type"):
            route_query(_state(router=router))

    def test_graph_registers_execute_task_node(self):
        from agentix.orchestration.langgraph import lg_builder

        nodes = set(lg_builder.build_graph(checkpointer=None).get_graph().nodes)
        assert "execute_task" in nodes

    def test_route_handlers_covers_execute_task(self):
        from agentix.orchestration.langgraph.lg_supervisor import ROUTE_HANDLERS

        assert "execute_task" in ROUTE_HANDLERS
        assert ROUTE_HANDLERS["execute_task"] is execute_task

    def test_route_query_literal_covers_execute_task(self):
        import typing

        hints = typing.get_type_hints(route_query)
        literal_args = hints["return"].__args__
        assert "execute_task" in literal_args


# ---------------------------------------------------------------------------
# 3. 写工具（声明 / 治理 / 落产物）
# ---------------------------------------------------------------------------


class TestWriteTools:
    @pytest.mark.asyncio
    async def test_flag_on_registers_specs_and_handlers(self, task_on):
        registry_init.ensure_registered()
        ex = get_tool_executor()
        for name in _TASK_TOOLS:
            assert tool_registry.has_tool(name)
            assert ex.has_handler(name)

    @pytest.mark.asyncio
    async def test_append_note_roundtrip(self, task_on):
        result = await get_tool_executor().execute(
            "append_task_note", {"note": "第一笔记录", "key": "p49"}
        )
        assert result.success, result.error
        data = result.data
        assert data["status"] == "stored"
        assert data["kind"] == "task_note"
        assert data["key"] == "p49"
        store = get_artifact_store()
        assert store.get(data["artifact_id"]) == "第一笔记录"
        ref = store._items[data["artifact_id"]][0]
        assert ref.kind == "task_note"
        assert ref.rebuildable is False  # 内存态：诚实声明不可重建
        assert ref.trace_id  # trace 可追溯

    @pytest.mark.asyncio
    async def test_append_note_validation_rejects(self, task_on):
        ex = get_tool_executor()
        empty = await ex.execute("append_task_note", {"note": "  "})
        assert not empty.success
        oversized = await ex.execute(
            "append_task_note", {"note": "x" * 4001}
        )
        assert not oversized.success

    @pytest.mark.asyncio
    async def test_control_chars_stripped(self, task_on):
        result = await get_tool_executor().execute(
            "append_task_note", {"note": "干净\x00文本\x07"}
        )
        assert result.success
        stored = get_artifact_store().get(result.data["artifact_id"])
        assert stored == "干净文本"

    @pytest.mark.asyncio
    async def test_write_artifact_roundtrip(self, task_on):
        result = await get_tool_executor().execute(
            "write_task_artifact",
            {
                "name": "p49_report.md",
                "content": "# 任务产物\n正文内容",
                "kind": "report",
                "summary": "e2e 摘要",
            },
        )
        assert result.success, result.error
        data = result.data
        assert data["name"] == "p49_report.md"
        store = get_artifact_store()
        assert store.get(data["artifact_id"]) == "# 任务产物\n正文内容"
        ref = store._items[data["artifact_id"]][0]
        assert ref.rebuildable is False
        assert ref.meta.get("name") == "p49_report.md"

    @pytest.mark.asyncio
    async def test_write_artifact_validation_rejects(self, task_on):
        ex = get_tool_executor()
        no_name = await ex.execute(
            "write_task_artifact", {"name": "", "content": "x"}
        )
        assert not no_name.success
        no_content = await ex.execute(
            "write_task_artifact", {"name": "a", "content": ""}
        )
        assert not no_content.success

    @pytest.mark.asyncio
    async def test_write_artifact_hitl_review_required(self, monkeypatch, task_on):
        """gate-on 时 HIGH 写入须审批：handler 绝不预跑（fail-closed）。"""
        monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True)
        reset_review_gate()
        calls: list = []
        ex = get_tool_executor()
        spec = tool_registry.get_spec("write_task_artifact")

        async def _spy(args: dict) -> dict:
            calls.append(dict(args))
            return {"ok": True}

        try:
            ex.register_handler("write_task_artifact", _spy, spec=spec)
            result = await ex.execute(
                "write_task_artifact", {"name": "x", "content": "y"}
            )
            assert not result.success
            assert result.metadata.get("reason") == "review_required"
            assert calls == []  # 审批前 handler 绝不执行
        finally:
            reset_review_gate()

    @pytest.mark.asyncio
    async def test_append_note_is_medium_in_gate(self, task_on):
        from agentix.tools.gateway.human_review import (
    declared_level_for,
    effective_risk_level,
)

        level = effective_risk_level(
            "append_task_note", {"note": "x"}, declared_risk_level="L1"
        )
        assert level.value == "medium"
        assert declared_level_for("L2").value == "high"


# ---------------------------------------------------------------------------
# 4. 任务管线（四终态 + 波次 + 拆解双路径）
# ---------------------------------------------------------------------------


def _happy_steps() -> list:
    return [
        {
            "id": "s1",
            "tool": "append_task_note",
            "args": {"note": "任务开始", "key": "p49"},
            "depends_on": [],
            "rationale": "先留痕",
        },
        {
            "id": "s2",
            "tool": "write_task_artifact",
            "args": {
                "name": "p49.md",
                "content": "任务产物正文",
                "kind": "report",
            },
            "depends_on": ["s1"],
            "rationale": "再产出",
        },
    ]


class TestTaskPipeline:
    @pytest.mark.asyncio
    async def test_declared_steps_happy_path_completed(self, task_on):
        result = await run_task_execution(
            "记录要点并产出一份报告", session_id="p49-sess",
            task_steps=_happy_steps(),
        )
        assert result.terminal == "completed"
        assert result.passed is True
        assert result.steps_used == 2
        assert len(result.observations) == 2
        assert all(o.ok for o in result.observations)
        assert "任务执行结果：2/2 步成功" in result.final
        store = get_artifact_store()
        kinds = [ref.kind for ref, _ in store._items.values()]
        assert kinds == ["task_note", "report"]  # 依赖链：s1 → s2（kind 来自入参）

    @pytest.mark.asyncio
    async def test_parallel_wave_two_independent_steps(self, task_on):
        steps = [
            {"id": "a", "tool": "append_task_note",
             "args": {"note": "笔记 A"}, "depends_on": []},
            {"id": "b", "tool": "append_task_note",
             "args": {"note": "笔记 B"}, "depends_on": []},
        ]
        result = await run_task_execution("两条并行笔记", session_id="p49-par",
                                          task_steps=steps)
        assert result.terminal == "completed"
        assert len(result.observations) == 2
        # 同一波次（meta.wave 相同）→ 声明式并行
        assert {o.meta.get("wave") for o in result.observations} == {0}

    @pytest.mark.asyncio
    async def test_llm_decompose_path_used(self, task_on, monkeypatch):
        seen: dict = {}

        async def _fake_llm(message, menu, max_steps):
            seen["message"] = message
            seen["menu"] = set(menu)
            seen["max_steps"] = max_steps
            return [
                {"id": "s1", "tool": "append_task_note",
                 "args": {"note": "LLM 拆解步骤"}}
            ]

        monkeypatch.setattr(task_execution_mod, "_llm_decompose", _fake_llm)
        result = await run_task_execution("帮我记一条笔记", session_id="p49-llm")
        assert result.terminal == "completed"
        assert seen["message"] == "帮我记一条笔记"
        assert "append_task_note" in seen["menu"]
        assert seen["max_steps"] == max(1, int(settings.MAX_ITERATIONS))

    @pytest.mark.asyncio
    async def test_unknown_tool_decompose_failed(self, task_on):
        steps = [{"id": "s1", "tool": "not_a_real_tool", "args": {}}]
        result = await run_task_execution("坏步骤", session_id="p49-bad",
                                          task_steps=steps)
        assert result.terminal == "failed"
        assert result.passed is False
        assert "decompose_failed" in result.reason
        assert "not_a_real_tool" in result.reason

    @pytest.mark.asyncio
    async def test_empty_decompose_failed(self, task_on, monkeypatch):
        async def _empty(message, menu, max_steps):
            return []

        monkeypatch.setattr(task_execution_mod, "_llm_decompose", _empty)
        result = await run_task_execution("无法拆解", session_id="p49-empty")
        assert result.terminal == "failed"
        assert "decompose_failed" in result.reason

    @pytest.mark.asyncio
    async def test_budget_exceeded_stops_after_budget(
        self, task_on, monkeypatch
    ):
        monkeypatch.setattr(settings, "MAX_ITERATIONS", 1)
        result = await run_task_execution("预算测试", session_id="p49-budget",
                                          task_steps=_happy_steps())
        assert result.terminal == "budget_exceeded"
        assert result.steps_used == 1
        assert result.passed is False
        assert "budget_exceeded" in result.reason
        # 只有 s1 落了产物（s2 未执行）
        assert len(get_artifact_store()._items) == 1

    @pytest.mark.asyncio
    async def test_all_fail_verify_failed_fast(self, task_on):
        """结构性全失败 → verify_failed；non-retryable 错误不重试（快速）。"""
        spec = ToolSpec(
            name="p49_failing_tool",
            risk_level="L0",
            description="always fails (P1 e2e)",
            parameters={},
        )
        tool_registry.register(spec, lambda: None)

        async def _boom(args: dict) -> dict:
            raise ValueError("invalid_request 400: cannot execute")

        get_tool_executor().register_handler("p49_failing_tool", _boom, spec=spec)
        try:
            result = await run_task_execution(
                "必败任务", session_id="p49-fail",
                task_steps=[{"id": "s1", "tool": "p49_failing_tool",
                             "args": {"x": 1}}],
            )
            assert result.terminal == "verify_failed"
            assert result.passed is False
            assert len(result.observations) == 1
            obs = result.observations[0]
            assert obs.ok is False
            assert obs.failure_layer is not None
            # non-retryable（invalid_request）→ 单次尝试，无退避等待
            assert obs.meta.get("attempts") == 1
        finally:
            tool_registry.delete_tool("p49_failing_tool")

    @pytest.mark.asyncio
    async def test_hitl_blocks_all_steps_verify_failed(
        self, task_on, monkeypatch
    ):
        monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True)
        reset_review_gate()
        store_before = set(get_artifact_store()._items)
        try:
            result = await run_task_execution(
                "写产物（应被 HITL 拦）", session_id="p49-hitl",
                task_steps=[{
                    "id": "s1", "tool": "write_task_artifact",
                    "args": {"name": "x", "content": "y"},
                }],
            )
            assert result.terminal == "verify_failed"
            obs = result.observations[0]
            assert obs.ok is False
            assert "human review" in obs.content.lower()
            # 审批前绝不落产物
            assert set(get_artifact_store()._items) == store_before
        finally:
            reset_review_gate()

    @pytest.mark.asyncio
    async def test_flag_off_pipeline_disabled(self, task_off):
        result = await run_task_execution(
            "任意任务", session_id="p49-off", task_steps=_happy_steps()
        )
        assert result.terminal == "failed"
        assert result.reason == "task_exec_disabled"
        assert result.passed is False


# ---------------------------------------------------------------------------
# 5. execute_task 图节点（flag-off no-op / flag-on 状态回写）
# ---------------------------------------------------------------------------


class TestExecuteTaskNode:
    @pytest.mark.asyncio
    async def test_flag_off_node_is_noop(self, task_off):
        state = _state("记一条笔记")
        out = await execute_task(state, config={"configurable": {}})
        assert out == {}

    @pytest.mark.asyncio
    async def test_flag_on_node_returns_message_and_router(self, task_on):
        state = _state("记录要点并产出报告")
        config = {
            "configurable": {"thread_id": "p49-node", "task_steps": _happy_steps()}
        }
        out = await execute_task(state, config=config)
        assert set(out) == {"messages", "router", "sources"}

        ai = out["messages"][0]
        assert "2/2 步成功" in ai.content
        assert ai.tool_calls  # 逐步工具轨迹可被 chat_service 读取
        names = [tc["name"] for tc in ai.tool_calls]
        assert names == ["append_task_note", "write_task_artifact"]
        assert all("id" in tc for tc in ai.tool_calls)
        assert ai.additional_kwargs.get("degraded") is False

        router = out["router"]
        assert isinstance(router, TaskRouter)
        assert router.type == "task-query"
        assert router.logic.startswith("task_exec:completed")
        assert router.question == "记录要点并产出报告"
        assert out["sources"] == []

    @pytest.mark.asyncio
    async def test_flag_on_node_degrades_honestly(self, task_on):
        state = _state("无法拆解的任务")
        config = {
            "configurable": {
                "thread_id": "p49-node-bad",
                "task_steps": [{"id": "s1", "tool": "unknown_tool", "args": {}}],
            }
        }
        out = await execute_task(state, config=config)
        ai = out["messages"][0]
        assert ai.additional_kwargs.get("degraded") is True
        assert out["router"].logic.startswith("task_exec:failed")


# ---------------------------------------------------------------------------
# 6. AGENT_MAX_REPLANS：run_agent_loop_ask 的 RetryReplanner 接线
# ---------------------------------------------------------------------------


async def _boom_tool(*, task, step, context):
    raise RuntimeError("invalid_request: tool always fails")


class TestAgentMaxReplans:
    @pytest.mark.asyncio
    async def test_default_zero_no_replan(self, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_MAX_REPLANS", 0)
        out = await run_agent_loop_ask(
            "失败任务", "p49-replan-off", extra_tools={"act": _boom_tool}
        )
        assert out["agent_loop"]["replans"] == 0
        assert out["agent_loop"]["terminal"] in {"verify_failed", "failed"}
        assert out["degraded"] is True

    @pytest.mark.asyncio
    async def test_flag_on_replans_at_least_once(self, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_MAX_REPLANS", 2)
        out = await run_agent_loop_ask(
            "失败任务", "p49-replan-on", extra_tools={"act": _boom_tool}
        )
        assert out["agent_loop"]["replans"] >= 1
        assert out["degraded"] is True
