"""P162（LG-3-4 恢复期幂等）：已完成步骤落 ``run_step_ledger``，同作用域重入跳过。

验收口径（设计文档 §4.4 LG-3-4 行）：**恢复一次后副作用只发生一次**——用计数桩
数调用次数；与 ``tests/test_p143_run_idempotency.py`` 的**客户端**幂等分层
（那层靠调用方传幂等键，本层靠服务端清单），互不援引。

固定面：

1. **store 单元**：roundtrip / ``merge`` 幂等 / 坏载荷按未命中 / 空入参 no-op /
   ``ensure_ready`` 建表 / ``attempts`` 下限；
2. **步骤身份**：``_assign_step_keys`` 只认 (tool, 规范化 args) 指纹，不认模型
   自拟 id；同计划内完全相同的步骤按出现序号区分；跨轮次稳定；
3. **开关与键门控**：``RECOVERY_STEP_LEDGER_ENABLED`` 关或 (session, scope)
   缺一 ⇒ 不碰 store（flag-off 逐位等价，约定 1）；
4. **恢复跳过**：全完成 / 部分完成 / 换 scope / 换会话 / 换 id / 重复步骤；
5. **fail-open**：读失败降级为「无恢复保护」并留 warning，写失败不影响终态
   （约定 2）；
6. **节点取作用域**：``execute_task`` 把 ``configurable["checkpoint_ns"]`` 透传为
   ``step_scope``；并用一条 langgraph 1.2.18 实测钉住该值的语义——同一次挂起
   续跑不变、新一轮对话换新值（本机制的作用域定义就建立在这条事实上）。
"""

from __future__ import annotations

from typing import Any, Dict, List, TypedDict

import pytest
from loguru import logger
from langchain_core.messages import HumanMessage
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import agentix.application.services.task_execution as task_execution_mod
import agentix.infrastructure.persistence.step_ledger_store as step_ledger_mod
from agentix.application.agents.task_handler import execute_task
from agentix.application.services.task_execution import (
    TaskStepDecl,
    _assign_step_keys,
    _open_step_ledger,
    run_task_execution,
)
from agentix.config.settings import settings
from agentix.infrastructure.persistence.db.models.run_step_ledger import (
    RunStepLedgerEntry,
)
from agentix.infrastructure.persistence.step_ledger_store import (
    StepLedgerStore,
    get_step_ledger_store,
    reset_step_ledger_store,
)
from agentix.orchestration.langgraph.lg_states import AgentState
from agentix.tools.definitions import ToolSpec, registry as tool_registry
from agentix.tools.gateway.executor import get_tool_executor, reset_tool_executor

_TOOL = "p162_counter"


# ---------------------------------------------------------------------------
# 夹具：sqlite 内存 store（不触真 Postgres）+ 计数桩工具
# ---------------------------------------------------------------------------


def _sqlite_store() -> StepLedgerStore:
    """StaticPool 单连接内存库（与 test_p136 同款范式）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    store = StepLedgerStore(session_factory=sessionmaker(bind=engine, expire_on_commit=False))
    store.ensure_ready()
    return store


def _counter_handler(calls: List[str]):
    """计数桩：记录每次真实执行；``tag == "boom"`` 走 non-retryable 失败。"""

    async def _handler(args: Dict[str, Any]) -> Dict[str, Any]:
        tag = str(args.get("tag") or "")
        calls.append(tag)
        if tag == "boom":
            raise ValueError("invalid_request 400: cannot execute")
        return {"echo": tag, "seq": len(calls)}

    return _handler


def _install_counter_tool(calls: List[str]) -> ToolSpec:
    """注册计数桩到 registry + executor，返回声明（菜单由夹具收敛）。"""
    spec = ToolSpec(
        name=_TOOL,
        description="p162 计数桩（恢复期幂等验收）",
        parameters={
            "type": "object",
            "properties": {"tag": {"type": "string"}},
        },
        risk_level="L0",
    )
    get_tool_executor().register_handler(_TOOL, _counter_handler(calls), spec=spec)
    return spec


@pytest.fixture
def ledger_on(monkeypatch: pytest.MonkeyPatch):
    """开关全开：SQLite 内存清单 + 计数桩。yield ``(calls, store)``。"""
    monkeypatch.setattr(settings, "TASK_EXEC_ENABLED", True)
    monkeypatch.setattr(settings, "RECOVERY_STEP_LEDGER_ENABLED", True)
    store = _sqlite_store()
    reset_step_ledger_store(session_factory=store._session_factory)
    reset_tool_executor()
    calls: List[str] = []
    spec = _install_counter_tool(calls)
    monkeypatch.setattr(task_execution_mod, "_executable_menu", lambda: {_TOOL: spec})
    try:
        yield calls, get_step_ledger_store()
    finally:
        tool_registry.delete_tool(_TOOL)
        reset_tool_executor()
        reset_step_ledger_store()


@pytest.fixture
def ledger_off(monkeypatch: pytest.MonkeyPatch):
    """业务开、清单关：并让任何取 store 的调用当场炸（证明零接触）。"""
    monkeypatch.setattr(settings, "TASK_EXEC_ENABLED", True)
    monkeypatch.setattr(settings, "RECOVERY_STEP_LEDGER_ENABLED", False)

    def _forbidden() -> StepLedgerStore:
        raise AssertionError("flag-off 不许打开 step ledger")

    monkeypatch.setattr(step_ledger_mod, "get_step_ledger_store", _forbidden)
    reset_tool_executor()
    calls: List[str] = []
    spec = _install_counter_tool(calls)
    monkeypatch.setattr(task_execution_mod, "_executable_menu", lambda: {_TOOL: spec})
    try:
        yield calls
    finally:
        tool_registry.delete_tool(_TOOL)
        reset_tool_executor()


def _steps(*tags: str, ids: List[str] | None = None) -> List[Dict[str, Any]]:
    """无依赖步骤（同一波次并行）；``ids`` 缺省 s1..sN。"""
    return [
        {
            "id": ids[i] if ids is not None else f"s{i + 1}",
            "tool": _TOOL,
            "args": {"tag": tag},
            "depends_on": [],
        }
        for i, tag in enumerate(tags)
    ]


def _decls(specs: List[tuple[str, Dict[str, Any], str]]) -> List[TaskStepDecl]:
    """``[(id, args, tool)]`` → TaskStepDecl 列表。"""
    return [TaskStepDecl(id=step_id, tool=tool, args=args) for step_id, args, tool in specs]


# ---------------------------------------------------------------------------
# 1. store 单元
# ---------------------------------------------------------------------------


class TestStepLedgerStore:
    def test_roundtrip_load_scope(self):
        store = _sqlite_store()
        written = store.mark_many(
            "th-1",
            "ns-1",
            [
                {"step_key": "k1", "tool": "t", "value": {"a": 1}, "attempts": 2},
                {"step_key": "k2", "tool": "t", "value": "plain", "attempts": 1},
            ],
        )
        assert written == 2
        done = store.load_scope("th-1", "ns-1")
        assert set(done) == {"k1", "k2"}
        assert done["k1"] == {"tool": "t", "value": {"a": 1}, "attempts": 2}
        assert done["k2"]["value"] == "plain"

    def test_merge_upsert_is_idempotent(self):
        store = _sqlite_store()
        store.mark_many(
            "th", "ns", [{"step_key": "k", "tool": "t", "value": {"v": 1}, "attempts": 1}]
        )
        store.mark_many(
            "th", "ns", [{"step_key": "k", "tool": "t", "value": {"v": 2}, "attempts": 3}]
        )
        done = store.load_scope("th", "ns")
        assert len(done) == 1
        assert done["k"]["value"] == {"v": 2}
        assert done["k"]["attempts"] == 3

    def test_corrupt_payload_treated_as_miss(self):
        store = _sqlite_store()
        factory = store._session_factory
        with factory() as session:
            session.add(
                RunStepLedgerEntry(
                    thread_id="th",
                    scope="ns",
                    step_key="broken",
                    tool="t",
                    value_json="{not-json",
                    attempts=1,
                    created_at=0.0,
                )
            )
            session.commit()
        assert store.load_scope("th", "ns") == {}

    def test_empty_inputs_are_noops(self):
        store = _sqlite_store()
        entry = [{"step_key": "k", "tool": "t", "value": 1, "attempts": 1}]
        assert store.mark_many("", "ns", entry) == 0
        assert store.mark_many("th", "", entry) == 0
        assert store.mark_many("th", "ns", []) == 0
        assert store.load_scope("", "ns") == {}
        assert store.load_scope("th", "") == {}

    def test_ensure_ready_creates_table(self):
        store = _sqlite_store()
        engine = store._session_factory.kw["bind"]
        assert inspect(engine).has_table("run_step_ledger")
        # 幂等：再次调用不抛
        store.ensure_ready()

    def test_attempts_floored_to_one(self):
        store = _sqlite_store()
        store.mark_many("th", "ns", [{"step_key": "k", "tool": "t", "value": 1, "attempts": 0}])
        assert store.load_scope("th", "ns")["k"]["attempts"] == 1

    def test_scopes_are_isolated(self):
        store = _sqlite_store()
        store.mark_many("th", "ns-a", [{"step_key": "k", "tool": "t", "value": "a", "attempts": 1}])
        store.mark_many("th", "ns-b", [{"step_key": "k", "tool": "t", "value": "b", "attempts": 1}])
        assert store.load_scope("th", "ns-a")["k"]["value"] == "a"
        assert store.load_scope("th", "ns-b")["k"]["value"] == "b"
        assert store.load_scope("other", "ns-a") == {}


# ---------------------------------------------------------------------------
# 2. 步骤身份
# ---------------------------------------------------------------------------


class TestStepIdentity:
    def test_keys_stable_across_independent_builds(self):
        first = _decls([("s1", {"tag": "a"}, _TOOL), ("s2", {"tag": "b"}, _TOOL)])
        second = _decls([("s1", {"tag": "a"}, _TOOL), ("s2", {"tag": "b"}, _TOOL)])
        assert _assign_step_keys(first) == _assign_step_keys(second)

    def test_keys_ignore_model_ids_and_order(self):
        a = _decls([("s1", {"tag": "a"}, _TOOL), ("s2", {"tag": "b"}, _TOOL)])
        b = _decls([("x2", {"tag": "b"}, _TOOL), ("x1", {"tag": "a"}, _TOOL)])
        keys_a, keys_b = _assign_step_keys(a), _assign_step_keys(b)
        # id 不同 ⇒ 映射键不同；但同一 (tool,args) 的身份值必须一致
        assert set(keys_a.values()) == set(keys_b.values())

    def test_duplicate_steps_get_distinct_occurrences(self):
        decls = _decls([("s1", {"tag": "dup"}, _TOOL), ("s2", {"tag": "dup"}, _TOOL)])
        keys = _assign_step_keys(decls)
        assert keys["s1"] != keys["s2"]
        assert keys["s1"].endswith(":0")
        assert keys["s2"].endswith(":1")

    def test_args_or_tool_change_changes_fingerprint(self):
        base = _assign_step_keys(_decls([("s1", {"tag": "a"}, _TOOL)]))["s1"]
        args_changed = _assign_step_keys(_decls([("s1", {"tag": "a2"}, _TOOL)]))["s1"]
        tool_changed = _assign_step_keys(_decls([("s1", {"tag": "a"}, "p162_other")]))["s1"]
        assert base != args_changed
        assert base != tool_changed

    def test_args_key_order_irrelevant(self):
        a = _assign_step_keys(_decls([("s1", {"tag": "a", "n": 1}, _TOOL)]))["s1"]
        b = _assign_step_keys(_decls([("s1", {"n": 1, "tag": "a"}, _TOOL)]))["s1"]
        assert a == b


# ---------------------------------------------------------------------------
# 3. 开关与键门控
# ---------------------------------------------------------------------------


class TestLedgerGating:
    def test_open_requires_flag_and_keys(self, ledger_on, monkeypatch):
        _, store = ledger_on
        assert _open_step_ledger("th", "ns") is store
        assert _open_step_ledger("", "ns") is None
        assert _open_step_ledger("th", "") is None
        monkeypatch.setattr(settings, "RECOVERY_STEP_LEDGER_ENABLED", False)
        assert _open_step_ledger("th", "ns") is None

    @pytest.mark.asyncio
    async def test_flag_off_pipeline_never_touches_store(self, ledger_off):
        calls = ledger_off
        first = await run_task_execution(
            "记录", session_id="p162-off", task_steps=_steps("a"), step_scope="ns"
        )
        second = await run_task_execution(
            "记录", session_id="p162-off", task_steps=_steps("a"), step_scope="ns"
        )
        assert first.terminal == "completed"
        assert second.terminal == "completed"
        # 无清单 ⇒ 两次都真实执行（基线行为，flag-off 逐位等价）
        assert calls == ["a", "a"]


# ---------------------------------------------------------------------------
# 4. 恢复跳过（计数桩验证「副作用只发生一次」）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRecoveryReplay:
    async def test_resume_skips_all_completed_steps(self, ledger_on):
        calls, store = ledger_on
        steps = _steps("a", "b")
        first = await run_task_execution(
            "两步", session_id="p162-all", task_steps=steps, step_scope="ns-1"
        )
        assert first.terminal == "completed"
        assert calls == ["a", "b"]
        assert len(store.load_scope("p162-all", "ns-1")) == 2

        second = await run_task_execution(
            "两步", session_id="p162-all", task_steps=steps, step_scope="ns-1"
        )
        assert second.terminal == "completed"
        assert calls == ["a", "b"]  # 零新增副作用
        assert second.steps_used == 2
        assert [o.ok for o in second.observations] == [True, True]
        assert '"echo": "a"' in second.observations[0].content

    async def test_partial_completion_skips_only_successful(self, ledger_on):
        calls, store = ledger_on
        steps = _steps("a", "boom")
        first = await run_task_execution(
            "部分完成", session_id="p162-part", task_steps=steps, step_scope="ns-p"
        )
        # 部分失败从 L1 通过、L2 DEGRADED ⇒ 仍算 completed（附带降级）
        assert first.terminal == "completed"
        assert first.passed is True
        assert calls == ["a", "boom"]
        keys = _assign_step_keys(
            _decls([("s1", {"tag": "a"}, _TOOL), ("s2", {"tag": "boom"}, _TOOL)])
        )
        done = store.load_scope("p162-part", "ns-p")
        assert set(done) == {keys["s1"]}  # 失败步骤不落账

        second = await run_task_execution(
            "部分完成", session_id="p162-part", task_steps=steps, step_scope="ns-p"
        )
        assert second.terminal == "completed"
        assert calls == ["a", "boom", "boom"]  # 只重跑失败步骤
        assert second.observations[0].ok is True
        assert second.observations[1].ok is False

    async def test_new_scope_reruns_everything(self, ledger_on):
        calls, _ = ledger_on
        steps = _steps("a")
        await run_task_execution(
            "换轮次", session_id="p162-scope", task_steps=steps, step_scope="ns-a"
        )
        await run_task_execution(
            "换轮次", session_id="p162-scope", task_steps=steps, step_scope="ns-b"
        )
        assert calls == ["a", "a"]

    async def test_other_session_not_skipped(self, ledger_on):
        calls, _ = ledger_on
        steps = _steps("a")
        await run_task_execution("换会话", session_id="p162-s1", task_steps=steps, step_scope="ns")
        await run_task_execution("换会话", session_id="p162-s2", task_steps=steps, step_scope="ns")
        assert calls == ["a", "a"]

    async def test_renamed_ids_still_skipped(self, ledger_on):
        calls, _ = ledger_on
        first = _steps("a", "b", ids=["s1", "s2"])
        second = _steps("a", "b", ids=["x9", "x8"])
        await run_task_execution(
            "恢复重拆", session_id="p162-ids", task_steps=first, step_scope="ns"
        )
        await run_task_execution(
            "恢复重拆", session_id="p162-ids", task_steps=second, step_scope="ns"
        )
        assert calls == ["a", "b"]

    async def test_identical_duplicate_steps_distinguished(self, ledger_on):
        calls, _ = ledger_on
        steps = _steps("dup", "dup")
        await run_task_execution(
            "重复步骤", session_id="p162-dup", task_steps=steps, step_scope="ns"
        )
        assert calls == ["dup", "dup"]
        second = await run_task_execution(
            "重复步骤", session_id="p162-dup", task_steps=steps, step_scope="ns"
        )
        assert calls == ["dup", "dup"]  # 两次都跳过，不合法地并成一次
        assert [o.ok for o in second.observations] == [True, True]

    async def test_no_scope_means_no_replay(self, ledger_on):
        calls, _ = ledger_on
        steps = _steps("a")
        await run_task_execution("缺作用域", session_id="p162-noscope", task_steps=steps)
        await run_task_execution("缺作用域", session_id="p162-noscope", task_steps=steps)
        assert calls == ["a", "a"]


# ---------------------------------------------------------------------------
# 5. fail-open（约定 2：辅助子系统故障只降级，不影响主链）
# ---------------------------------------------------------------------------


class _BrokenStore:
    def __init__(self, *, fail_load: bool = False, fail_mark: bool = False):
        self._fail_load = fail_load
        self._fail_mark = fail_mark

    def load_scope(self, thread_id: str, scope: str) -> Dict[str, Any]:
        if self._fail_load:
            raise RuntimeError("ledger db down")
        return {}

    def mark_many(self, thread_id: str, scope: str, entries: Any) -> int:
        if self._fail_mark:
            raise RuntimeError("ledger db down")
        return 0


def _capture_logs(level: str = "WARNING") -> tuple[list[str], int]:
    records: List[str] = []
    handler_id = logger.add(lambda m: records.append(str(m)), level=level)
    return records, handler_id


@pytest.mark.asyncio
class TestFailOpen:
    async def test_load_failure_degrades_and_warns(self, ledger_on, monkeypatch):
        calls, _ = ledger_on
        broken = _BrokenStore(fail_load=True)
        monkeypatch.setattr(step_ledger_mod, "get_step_ledger_store", lambda: broken)
        records, handler_id = _capture_logs()
        try:
            result = await run_task_execution(
                "读失败",
                session_id="p162-load",
                task_steps=_steps("a"),
                step_scope="ns",
            )
        finally:
            logger.remove(handler_id)
        assert result.terminal == "completed"
        assert calls == ["a"]
        assert any("step ledger load failed" in r for r in records)

    async def test_mark_failure_does_not_break_task(self, ledger_on, monkeypatch):
        calls, _ = ledger_on
        broken = _BrokenStore(fail_mark=True)
        monkeypatch.setattr(step_ledger_mod, "get_step_ledger_store", lambda: broken)
        records, handler_id = _capture_logs()
        try:
            result = await run_task_execution(
                "写失败",
                session_id="p162-mark",
                task_steps=_steps("a"),
                step_scope="ns",
            )
        finally:
            logger.remove(handler_id)
        assert result.terminal == "completed"
        assert calls == ["a"]
        assert any("step ledger mark failed" in r for r in records)


# ---------------------------------------------------------------------------
# 6. 节点取作用域 + langgraph 事实钉住
# ---------------------------------------------------------------------------


class _Captured(Exception):
    """哨兵：在 run_task_execution 被调用处跳出，只验证透传。"""


class TestExecuteTaskScope:
    @pytest.mark.asyncio
    async def test_passes_checkpoint_ns_as_step_scope(self, monkeypatch):
        monkeypatch.setattr(settings, "TASK_EXEC_ENABLED", True)
        captured: Dict[str, Any] = {}

        async def _fake(message: str, **kwargs: Any):
            captured.update(kwargs)
            captured["message"] = message
            raise _Captured()

        monkeypatch.setattr(task_execution_mod, "run_task_execution", _fake)
        state = AgentState(messages=[HumanMessage(content="任务")])
        config = {
            "configurable": {
                "thread_id": "p162-node",
                "checkpoint_ns": "execute_task:deadbeef",
            }
        }
        with pytest.raises(_Captured):
            await execute_task(state, config=config)
        assert captured["step_scope"] == "execute_task:deadbeef"
        assert captured["session_id"] == "p162-node"

    @pytest.mark.asyncio
    async def test_missing_checkpoint_ns_passes_empty_scope(self, monkeypatch):
        monkeypatch.setattr(settings, "TASK_EXEC_ENABLED", True)
        captured: Dict[str, Any] = {}

        async def _fake(message: str, **kwargs: Any):
            captured.update(kwargs)
            raise _Captured()

        monkeypatch.setattr(task_execution_mod, "run_task_execution", _fake)
        state = AgentState(messages=[HumanMessage(content="任务")])
        with pytest.raises(_Captured):
            await execute_task(state, config={"configurable": {"thread_id": "p162-node2"}})
        assert captured["step_scope"] == ""


class _NsState(TypedDict, total=False):
    seen: list


@pytest.mark.asyncio
async def test_checkpoint_ns_stable_on_resume_new_per_turn():
    """1.2.18 事实：同一挂起任务的续跑 ns 相同，新一轮对话换新 ns。

    ``execute_task`` 的 ``step_scope`` 就取这个值——若这条事实变了，清单会
    要么跨轮次误跳（旧成绩当新任务），要么恢复时全部失配（等于没有保护）。
    """
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt

    seen_ns: List[str] = []

    def _node(state: _NsState, config: RunnableConfig) -> Dict[str, Any]:
        ns = str((config.get("configurable") or {}).get("checkpoint_ns") or "")
        seen_ns.append(ns)
        decision = interrupt({"ask": ns})
        return {"seen": [*state.get("seen", []), f"{ns}|{decision}"]}

    graph = StateGraph(_NsState)
    graph.add_node("n", _node)
    graph.add_edge(START, "n")
    graph.add_edge("n", END)
    app = graph.compile(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "p162-ns"}}

    first = await app.ainvoke({"seen": []}, cfg)
    assert first.get("__interrupt__"), "首次调用应挂起"
    ns1 = seen_ns[0]
    assert ns1  # 节点级 ns 非空（根 ""，节点形如 n:<uuid>）

    resumed = await app.ainvoke(Command(resume="ok"), cfg)
    assert all(ns == ns1 for ns in seen_ns), f"续跑 ns 漂移: {seen_ns}"
    assert resumed["seen"] == [f"{ns1}|ok"]

    second = await app.ainvoke({"seen": []}, cfg)
    assert second.get("__interrupt__")
    ns2 = seen_ns[-1]
    assert ns2 != ns1  # 新一轮对话换新作用域
    await app.ainvoke(Command(resume="ok2"), cfg)
