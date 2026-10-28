"""F1c 验收（统一执行链 Stage F · MultiAgent P1 · A-P1-2 LLM 规划 + TodoWrite）。

覆盖 multiagent-optimization §5 的 A-P1-2：

1. **五处同步棘轮**（settings / selfcheck / .env.example / 配置管理.md /
   p41 手写清单），新键 ``AGENT_LLM_PLANNER_ENABLED`` 默认 False。
2. **LLMPlanner**（flag 门控）：flag-off 选择器仍产出 ``SingleStepPlanner``
   （与基线逐位等价）、flag-on 产出 ``LLMPlanner``（结构化输出、执行表
   白名单整步过滤、步数封顶、依赖裁剪、失败/空计划 fail-open 回单步语义）；
   ``RetryReplanner`` 内层注入仅在 flag-on；规划提示可携带 Todo 快照
   （provider 故障 fail-open）。
3. **todo_write 工具**：声明（L2+control、类名即 title）与 handler 成对
   flag 门控；整体替换 + 至多一条 ``in_progress`` + 校验诚实失败；
   ``TodoStore`` JSON 落盘（原子替换、损坏容忍、落盘失败内存态保留、
   跨实例可恢复）；回执即读通道；``main.py`` 装配源级断言。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List, Optional

import pytest
from pydantic import ValidationError

from agentix.application.services import agent_loop_adapter as adapter
from agentix.application.services import todo_write as todo_svc
from agentix.config.selfcheck import DEFAULT_IGNORED_MISSING
from agentix.config.settings import settings
from agentix.domain.agent.types import TaskSpec
from agentix.policies.planner import DeclaredStepsPlanner, RetryReplanner, SingleStepPlanner
from agentix.policies.planner import llm as llm_mod
from agentix.policies.planner.llm import LLMPlanner, LLMPlanOutput, PlanStepDraft
from agentix.tools import registry_init
from agentix.tools.adapters.todo_write import (
    MAX_TODO_CONTENT,
    TodoItem,
    build_todo_write_spec,
    todo_write as TodoWriteSchema,
)
from agentix.tools.definitions import registry as tool_registry
from agentix.tools.gateway.executor import (
    get_tool_executor,
    reset_tool_executor,
)
from agentix.tools.gateway.human_review import reset_review_gate

REPO = Path(__file__).resolve().parents[1]

#: F1c 新键（默认 False——任何测试都必须还原）
_KEY = "AGENT_LLM_PLANNER_ENABLED"
assert getattr(settings, _KEY) is False, f"{_KEY} 基线必须为 False"

#: 本批挂进声明面的工具（teardown 必须摘掉，防跨文件污染）
_NEW_TOOLS = ("todo_write",)


def _reset_decls() -> None:
    registry_init._DECLARATIONS.clear()
    for name in _NEW_TOOLS:
        tool_registry.delete_tool(name)


@pytest.fixture
def fresh_state():
    """声明缓存 / executor / review gate / todo store 四单例复位。"""
    _reset_decls()
    reset_tool_executor()
    reset_review_gate()
    todo_svc.set_todo_store(None)
    yield
    _reset_decls()
    reset_tool_executor()
    reset_review_gate()
    todo_svc.set_todo_store(None)


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(settings, _KEY, True)
    return settings


@pytest.fixture
def todo_env(tmp_path):
    """TodoStore 注入 tmp 路径（绝不写仓库 data/）。"""
    store = todo_svc.TodoStore(tmp_path / "todos" / "current.json")
    todo_svc.set_todo_store(store)
    yield store
    todo_svc.set_todo_store(None)


def _declared_names() -> set:
    return {spec.name for spec, _ in registry_init.get_declarations()}


class _FakePlanClient:
    """规划用 LLM 桩：捕获消息、按构造返回结构化输出或异常。"""

    def __init__(
        self,
        *,
        output: Optional[LLMPlanOutput] = None,
        exc: Optional[BaseException] = None,
    ) -> None:
        self.output = output
        self.exc = exc
        self.messages: List[Any] = []
        self.schema: Any = None

    def with_structured_output(self, schema: Any) -> "_FakePlanClient":
        self.schema = schema
        return self

    async def ainvoke(self, messages: List[Any]) -> Any:
        self.messages = list(messages)
        if self.exc is not None:
            raise self.exc
        return self.output


def _valid_output() -> LLMPlanOutput:
    return LLMPlanOutput(
        steps=[
            PlanStepDraft(id="a", action="act", rationale="第一步"),
            PlanStepDraft(id="b", action="final_answer", depends_on=["a"], rationale="汇总"),
        ]
    )


class TestFlagSync:
    """新键五处同步棘轮。"""

    def test_settings_default_off(self):
        assert getattr(settings, _KEY) is False

    def test_selfcheck_whitelist(self):
        assert _KEY in DEFAULT_IGNORED_MISSING

    def test_env_example_active_line(self):
        lines = (REPO / ".env.example").read_text(encoding="utf-8").splitlines()
        assert f"{_KEY}=false" in [line.strip() for line in lines]

    def test_configuration_md_documented(self):
        text = (REPO / "docs" / "architecture" / "配置管理.md").read_text(
            encoding="utf-8"
        )
        assert _KEY in text

    def test_p41_manual_list(self):
        text = (REPO / "tests" / "test_p41_doc_consistency.py").read_text(
            encoding="utf-8"
        )
        assert _KEY in text


class TestPlannerSelection:
    """组合根选择器：flag-off 与基线同型，flag-on 换 LLMPlanner。"""

    def test_flag_off_is_single_step(self):
        planner = adapter._select_planner(adapter.build_tool_map())
        assert isinstance(planner, SingleStepPlanner)

    def test_flag_on_is_llm_planner(self, flag_on):
        tools = adapter.build_tool_map()
        planner = adapter._select_planner(tools)
        assert isinstance(planner, LLMPlanner)
        assert planner._allowed == frozenset(tools)
        assert planner._max_steps == max(1, int(settings.MAX_ITERATIONS))
        assert callable(planner._todo_provider)

    def test_replanner_zero_returns_none(self):
        assert adapter._select_replanner(0) is None

    def test_replanner_flag_off_has_no_inner_planner(self):
        replanner = adapter._select_replanner(2)
        assert isinstance(replanner, RetryReplanner)
        assert replanner._planner is None  # 与基线接线逐位等价

    def test_replanner_flag_on_injects_inner_planner(self, flag_on):
        inner = adapter._select_planner(adapter.build_tool_map())
        replanner = adapter._select_replanner(2, planner=inner)
        assert isinstance(replanner, RetryReplanner)
        assert replanner._planner is inner

    def test_todo_snapshot_fail_open(self, fresh_state):
        # store 未注入 ⇒ 走默认路径（data/todos/current.json 不存在 ⇒ []）
        assert adapter._todo_snapshot() == []

    @pytest.mark.asyncio
    async def test_declared_fast_path_untouched(self):
        """DeclaredSteps 仍是确定性 fast-path（A-P1-2 不动它）。"""
        task = TaskSpec(goal="g", constraints={"steps": [{"id": "x", "action": "act"}]})
        plan = await DeclaredStepsPlanner().plan(task)
        assert plan.planner == "declared"
        assert [s.id for s in plan.steps] == ["x"]


class TestLLMPlannerBehavior:
    """LLMPlanner：结构化输出 → 白名单过滤 → fail-open 回单步。"""

    @pytest.mark.asyncio
    async def test_plan_success(self):
        fake = _FakePlanClient(output=_valid_output())
        planner = LLMPlanner(
            allowed_tools=frozenset({"act", "final_answer"}), max_steps=8, client=fake
        )
        plan = await planner.plan(TaskSpec(goal="评估合同风险并汇总"))
        assert plan.planner == "llm"
        assert [s.id for s in plan.steps] == ["a", "b"]
        assert plan.steps[1].depends_on == ("a",)
        assert fake.schema is LLMPlanOutput
        system, user = fake.messages[0]["content"], fake.messages[1]["content"]
        assert "评估合同风险并汇总" in user
        assert "'act'" in system and "'final_answer'" in system  # 白名单注入提示

    @pytest.mark.asyncio
    async def test_whitelist_drops_unknown_tool(self):
        fake = _FakePlanClient(
            output=LLMPlanOutput(
                steps=[
                    PlanStepDraft(id="a", action="act"),
                    PlanStepDraft(id="b", action="run", tool="shell"),
                ]
            )
        )
        planner = LLMPlanner(allowed_tools=frozenset({"act"}), client=fake)
        plan = await planner.plan(TaskSpec(goal="g"))
        assert plan.planner == "llm"
        assert [s.id for s in plan.steps] == ["a"]

    @pytest.mark.asyncio
    async def test_fail_open_on_exception(self):
        fake = _FakePlanClient(exc=RuntimeError("llm down"))
        planner = LLMPlanner(allowed_tools=frozenset({"act"}), client=fake)
        plan = await planner.plan(TaskSpec(goal="g"))
        assert plan.planner == "single_step"
        assert len(plan.steps) == 1 and plan.steps[0].action == "act"

    @pytest.mark.asyncio
    async def test_fail_open_on_empty_output(self):
        fake = _FakePlanClient(output=LLMPlanOutput(steps=[]))
        planner = LLMPlanner(allowed_tools=frozenset({"act"}), client=fake)
        plan = await planner.plan(TaskSpec(goal="g"))
        assert plan.planner == "single_step"

    @pytest.mark.asyncio
    async def test_fail_open_when_all_steps_filtered(self):
        fake = _FakePlanClient(
            output=LLMPlanOutput(steps=[PlanStepDraft(id="a", action="shell")])
        )
        planner = LLMPlanner(allowed_tools=frozenset({"act"}), client=fake)
        plan = await planner.plan(TaskSpec(goal="g"))
        assert plan.planner == "single_step"

    @pytest.mark.asyncio
    async def test_dedup_ids_and_prune_deps(self):
        fake = _FakePlanClient(
            output=LLMPlanOutput(
                steps=[
                    PlanStepDraft(id="a", action="act"),
                    PlanStepDraft(id="a", action="act", rationale="重复 id"),
                    PlanStepDraft(id="b", action="act", depends_on=["zz", "b"]),
                ]
            )
        )
        planner = LLMPlanner(allowed_tools=frozenset({"act"}), client=fake)
        plan = await planner.plan(TaskSpec(goal="g"))
        assert [s.id for s in plan.steps] == ["a", "b"]
        assert plan.steps[1].depends_on == ()  # 未知依赖与自引用都被裁掉

    @pytest.mark.asyncio
    async def test_max_steps_cap(self):
        fake = _FakePlanClient(
            output=LLMPlanOutput(
                steps=[
                    PlanStepDraft(id=str(i), action="act") for i in range(4)
                ]
            )
        )
        planner = LLMPlanner(allowed_tools=frozenset({"act"}), max_steps=2, client=fake)
        plan = await planner.plan(TaskSpec(goal="g"))
        assert [s.id for s in plan.steps] == ["0", "1"]

    @pytest.mark.asyncio
    async def test_todo_snapshot_in_prompt(self, todo_env):
        todo_env.replace([{"content": "梳理争议条款", "status": "in_progress"}])
        fake = _FakePlanClient(output=_valid_output())
        planner = LLMPlanner(
            allowed_tools=frozenset({"act"}),
            client=fake,
            todo_provider=adapter._todo_snapshot,
        )
        plan = await planner.plan(TaskSpec(goal="g"))
        assert plan.planner == "llm"
        user = fake.messages[1]["content"]
        assert "当前 TodoWrite 清单" in user
        assert "梳理争议条款" in user

    @pytest.mark.asyncio
    async def test_todo_provider_failure_fail_open(self):
        def _boom() -> Any:
            raise RuntimeError("store gone")

        fake = _FakePlanClient(output=_valid_output())
        planner = LLMPlanner(
            allowed_tools=frozenset({"act"}),
            client=fake,
            todo_provider=_boom,
        )
        plan = await planner.plan(TaskSpec(goal="g"))
        assert plan.planner == "llm"
        assert "当前 TodoWrite 清单" not in fake.messages[1]["content"]

    @pytest.mark.asyncio
    async def test_adapter_end_to_end_flag_on(self, flag_on, todo_env, monkeypatch):
        """选择器产出的 planner 走真实调用链（桩换 LLM 客户端单点）。"""
        todo_env.replace([{"content": "取证", "status": "pending"}])
        fake = _FakePlanClient(output=_valid_output())
        monkeypatch.setattr(llm_mod, "default_plan_client", lambda: fake)
        planner = adapter._select_planner(adapter.build_tool_map())
        plan = await planner.plan(TaskSpec(goal="合同审查"))
        assert plan.planner == "llm"
        user = fake.messages[1]["content"]
        assert "合同审查" in user
        assert "取证" in user


class TestTodoWriteSpecAndRegistry:
    """声明契约 + registry 双态（flag-off 不声明 / flag-on 声明）。"""

    def test_spec_contract(self):
        spec = build_todo_write_spec()
        assert spec.name == "todo_write"
        assert spec.risk_level == "L2"  # 词表 write → HIGH，两层一致
        assert spec.side_effect is True
        assert spec.control.strip()  # L2 必填控制措施
        assert spec.parameters["title"] == "todo_write"  # 类名即工具名

    def test_schema_validation(self):
        ok = TodoWriteSchema(
            items=[{"content": "a", "status": "pending"}]
        )
        assert ok.items[0].status == "pending"
        with pytest.raises(ValidationError):
            TodoWriteSchema(items=[{"content": "a", "status": "doing"}])
        with pytest.raises(ValidationError):
            TodoWriteSchema(items=[{"content": "", "status": "pending"}])
        with pytest.raises(ValidationError):
            TodoWriteSchema(items=[{"content": "x" * (MAX_TODO_CONTENT + 1), "status": "pending"}])
        assert TodoItem(content="c", status="completed").status == "completed"

    def test_registry_flag_off_not_declared(self, fresh_state):
        assert "todo_write" not in _declared_names()

    def test_registry_flag_on_declared(self, fresh_state, flag_on):
        assert "todo_write" in _declared_names()

    def test_main_install_is_flag_gated(self):
        text = (REPO / "agentix" / "main.py").read_text(encoding="utf-8")
        assert "install_todo_write_tool" in text
        assert 'if bool(getattr(settings, "AGENT_LLM_PLANNER_ENABLED", False)):' in text

    def test_install_belt_and_suspenders_flag_off(self, fresh_state):
        """调用方漏判 flag 时 install 自身早退（不挂 handler）。"""
        todo_svc.install_todo_write_tool()
        assert get_tool_executor().has_handler("todo_write") is False


class TestTodoWriteExecution:
    """executor 端到端：装配 → 执行 → 校验 → 回执。"""

    @pytest.mark.asyncio
    async def test_install_and_execute_roundtrip(self, fresh_state, flag_on, todo_env):
        todo_svc.install_todo_write_tool()
        executor = get_tool_executor()
        assert executor.has_handler("todo_write")
        result = await executor.execute(
            "todo_write",
            {
                "items": [
                    {"content": "整理证据", "status": "in_progress"},
                    {"content": "起草意见", "status": "pending"},
                ]
            },
        )
        assert result.success, result.error
        assert result.data["ok"] is True
        assert [i["status"] for i in result.data["items"]] == [
            "in_progress",
            "pending",
        ]
        assert result.data["version"] == 1
        # 回执即读通道 + 落盘可恢复
        on_disk = json.loads(todo_env.path.read_text(encoding="utf-8"))
        assert on_disk["items"] == result.data["items"]
        assert on_disk["version"] == 1

    @pytest.mark.asyncio
    async def test_execute_validation_errors(self, fresh_state, flag_on, todo_env):
        todo_svc.install_todo_write_tool()
        executor = get_tool_executor()

        bad_status = await executor.execute(
            "todo_write", {"items": [{"content": "a", "status": "doing"}]}
        )
        assert not bad_status.success
        assert "status" in bad_status.error

        double = await executor.execute(
            "todo_write",
            {
                "items": [
                    {"content": "a", "status": "in_progress"},
                    {"content": "b", "status": "in_progress"},
                ]
            },
        )
        assert not double.success
        assert "in_progress" in double.error

        missing = await executor.execute("todo_write", {})
        assert not missing.success
        assert "items" in missing.error

        too_many = await executor.execute(
            "todo_write",
            {
                "items": [
                    {"content": f"t{i}", "status": "pending"} for i in range(51)
                ]
            },
        )
        assert not too_many.success
        assert "too many" in too_many.error
        # 校验失败不落半份状态
        assert todo_svc.get_todo_store().snapshot()["version"] == 0


class TestTodoStore:
    """TodoStore：整体替换 / 版本递增 / 落盘恢复 / 损坏与落盘故障 fail-open。"""

    def test_replace_is_whole_list(self, todo_env):
        todo_env.replace([{"content": "a", "status": "pending"}])
        snap = todo_env.replace([{"content": "b", "status": "completed"}])
        assert snap["items"] == [{"content": "b", "status": "completed"}]
        assert snap["version"] == 2

    def test_persistence_recovery(self, todo_env):
        todo_env.replace([{"content": "跨进程可恢复", "status": "pending"}])
        reloaded = todo_svc.TodoStore(todo_env.path)
        assert reloaded.snapshot() == todo_env.snapshot()

    def test_corrupt_file_starts_empty(self, tmp_path):
        path = tmp_path / "todos" / "current.json"
        path.parent.mkdir(parents=True)
        path.write_text("{ not json", encoding="utf-8")
        store = todo_svc.TodoStore(path)
        assert store.snapshot() == {"items": [], "version": 0}
        snap = store.replace([{"content": "ok", "status": "pending"}])
        assert len(snap["items"]) == 1

    def test_persist_failure_keeps_memory(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file", encoding="utf-8")  # 目录位被占 → mkdir 失败
        store = todo_svc.TodoStore(blocker / "current.json")
        snap = store.replace([{"content": "kept", "status": "pending"}])
        assert snap["items"] == [{"content": "kept", "status": "pending"}]

    @pytest.mark.asyncio
    async def test_clear_list(self, todo_env):
        todo_env.replace([{"content": "a", "status": "pending"}])
        snap = todo_env.replace([])
        assert snap["items"] == []
        assert snap["version"] == 2

    @pytest.mark.asyncio
    async def test_handler_direct(self, fresh_state, todo_env):
        result = await todo_svc.todo_write_handler(
            {"items": [{"content": "直接", "status": "pending"}]}
        )
        assert result["ok"] is True
        assert result["items"][0]["content"] == "直接"
        with pytest.raises(ValueError):
            await todo_svc.todo_write_handler({})
