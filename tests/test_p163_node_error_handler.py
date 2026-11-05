"""LG-3-5 验收：节点级错误补偿（langgraph 1.2 ``add_node(error_handler=)``）。

## 覆盖与验收口径（指导文档 §4.4 LG-3-5「怎么算过」）

1. **关时逐位等价**：``NODE_ERROR_HANDLER_ENABLED=false``（默认）时编译图里
   没有任何 ``__error_handler__*`` 节点、``node_error_handler_map`` 为空——
   即 ``add_node`` **完全不传**该形参（约定 1）；静态守卫钉住
   ``lg_builder._node_error_handler_kwargs`` 的实参来自 ``policies``。
2. **开时重试耗尽进入 handler，``Command`` 能改 state 并完成路由**：同节点挂
   ``retry_policy``+``error_handler``，节点执行满 ``max_attempts`` 次后 handler
   才跑一次；handler 的 ``Command(update=..., goto=...)`` 被框架采纳（目标节点
   真被执行、state 真被改）。
3. **补偿记录复用 ``hitl_pending_approvals``（Q7 真源），不新建存储**：记录写进
   既有表，``status=rejected``（已闭合，不会被启动恢复当成幽灵审批）、
   ``decided_by=system:node_error_handler``、``decision_reason`` 含节点与异常；
   同一 (线程, 节点, 异常) 幂等收敛到一行。
4. **fail-open**：写库失败只告警，失败回执与路由照常（handler 自身抛错会让整轮
   失败，所以补偿动作必须自带兜底）。
5. **不重跑节点**：``execute_task`` / ``code_agent`` 开关打开后仍无
   ``retry_policy``（整节点重跑会重放工具副作用，LG-1-1 铁律）。
6. **HITL 兼容**：``GraphBubbleUp``（``interrupt`` 同族）不交给 handler——
   挂起语义不因补偿接线而改变。
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import pathlib
from typing import Any, Dict, List, TypedDict

import pytest
from langgraph.errors import GraphBubbleUp, NodeError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, RetryPolicy
from loguru import logger
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agentix.config import settings
from agentix.infrastructure.persistence.db.models.hitl_pending import (
    HitlPendingApproval,
)
from agentix.infrastructure.persistence.hitl_pending_store import HitlPendingStore
from agentix.orchestration.langgraph import lg_builder, policies
from agentix.orchestration.langgraph.policies import (
    NODE_ERROR_RECORD_DECIDED_BY,
    node_error_handler,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
LG_BUILDER_PATH = REPO_ROOT / "agentix" / "orchestration" / "langgraph" / "lg_builder.py"

#: 开关打开后**允许**挂 error_handler 的节点（有副作用，失败时整轮 500 代价最高）。
SIDE_EFFECT_NODES = ("execute_task", "code_agent")


# ---------------------------------------------------------------------------
# 测试载体：sqlite 内存库（StaticPool 单连接，与 test_p136/p162 同款范式）
# ---------------------------------------------------------------------------


def _sqlite_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    return sessionmaker(bind=engine, expire_on_commit=False)


def _hitl_store(factory) -> HitlPendingStore:
    store = HitlPendingStore(session_factory=factory)
    store.ensure_ready()
    return store


def _rows(factory) -> List[HitlPendingApproval]:
    with factory() as session:
        return list(session.execute(select(HitlPendingApproval)).scalars())


def _run(coro):
    return asyncio.run(coro)


def _invoke_handler(handler, *, node: str, exc: BaseException, thread_id: str = "t-1"):
    return _run(
        handler(
            {"messages": []},
            error=NodeError(node=node, error=exc),
            config={"configurable": {"thread_id": thread_id}},
        )
    )


class _State(TypedDict, total=False):
    status: str
    attempts: int


class _BrokenStore:
    """写库必失败的替身：验证 handler 的 fail-open 兜底。"""

    def close_decision(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("db down (p163)")


def _capture_logs(level: str = "WARNING"):
    records: List[str] = []
    handler_id = logger.add(lambda m: records.append(str(m)), level=level)
    return records, handler_id


# ---------------------------------------------------------------------------
# 1) 配置键与工厂契约
# ---------------------------------------------------------------------------


class TestSettingsKey:
    def test_default_is_off(self):
        assert settings.NODE_ERROR_HANDLER_ENABLED is False

    def test_factory_is_exported(self):
        assert callable(getattr(policies, "node_error_handler", None))
        assert "node_error_handler" in policies.__all__
        assert "NODE_ERROR_RECORD_DECIDED_BY" in policies.__all__


class TestHandlerContract:
    def test_returns_command_updating_messages_and_routing_to_end(self):
        factory = _sqlite_factory()
        handler = node_error_handler(store=_hitl_store(factory))
        cmd = _invoke_handler(handler, node="execute_task", exc=RuntimeError("boom"))
        assert isinstance(cmd, Command)
        assert cmd.goto == END
        messages = cmd.update["messages"]
        assert len(messages) == 1
        content = messages[0].content
        assert "execute_task" in content and "RuntimeError" in content
        assert "已记录补偿项" in content

    def test_custom_exit_node_is_honoured(self):
        factory = _sqlite_factory()
        handler = node_error_handler(
            store=_hitl_store(factory), exit_node="respond_to_general_query"
        )
        cmd = _invoke_handler(handler, node="code_agent", exc=RuntimeError("x"))
        assert cmd.goto == "respond_to_general_query"


# ---------------------------------------------------------------------------
# 2) 补偿记录：复用 hitl_pending 真源
# ---------------------------------------------------------------------------


class TestCompensationRecord:
    def test_writes_closed_record_reusing_hitl_pending(self):
        factory = _sqlite_factory()
        store = _hitl_store(factory)
        handler = node_error_handler(store=store)
        _invoke_handler(
            handler, node="execute_task", exc=ValueError("bad args"), thread_id="thread-9"
        )

        rows = _rows(factory)
        assert len(rows) == 1
        row = rows[0]
        assert row.status == "rejected", "补偿记录必须是已闭合态，否则启动恢复会造幽灵审批"
        assert row.decided_by == NODE_ERROR_RECORD_DECIDED_BY
        assert row.thread_id == "thread-9"
        assert row.tool_name == "execute_task"
        assert row.arguments["error_type"] == "ValueError"
        assert row.arguments["error"] == "bad args"
        assert "execute_task" in row.decision_reason
        assert "ValueError: bad args" in row.decision_reason
        assert row.decided_at is not None

    def test_same_failure_is_idempotent(self):
        factory = _sqlite_factory()
        handler = node_error_handler(store=_hitl_store(factory))
        _invoke_handler(handler, node="execute_task", exc=RuntimeError("same"))
        _invoke_handler(handler, node="execute_task", exc=RuntimeError("same"))
        assert len(_rows(factory)) == 1, "同一 (线程, 节点, 异常) 必须收敛到一行"

    def test_different_thread_or_error_makes_new_row(self):
        factory = _sqlite_factory()
        handler = node_error_handler(store=_hitl_store(factory))
        _invoke_handler(handler, node="execute_task", exc=RuntimeError("same"), thread_id="t-a")
        _invoke_handler(handler, node="execute_task", exc=RuntimeError("same"), thread_id="t-b")
        _invoke_handler(handler, node="execute_task", exc=RuntimeError("other"), thread_id="t-a")
        assert len(_rows(factory)) == 3

    def test_pending_row_is_never_used(self):
        """反向钉死：写出的行不得留在未决集合里（``list_unresolved`` 必须为空）。"""
        factory = _sqlite_factory()
        store = _hitl_store(factory)
        _invoke_handler(node_error_handler(store=store), node="code_agent", exc=RuntimeError("x"))
        assert store.list_unresolved() == []


class TestFailOpen:
    def test_store_failure_still_returns_failure_exit(self):
        records, handler_id = _capture_logs()
        try:
            cmd = _invoke_handler(
                node_error_handler(store=_BrokenStore()),
                node="execute_task",
                exc=RuntimeError("boom"),
            )
        finally:
            logger.remove(handler_id)
        assert isinstance(cmd, Command)
        assert cmd.goto == END
        assert (
            "补偿记录写入失败" in cmd.update["messages"][0].content
        ), "写库失败时回执必须如实说明，不能声称「已记录」"
        assert any("compensation record write failed" in r for r in records)

    def test_missing_thread_id_still_writes(self):
        factory = _sqlite_factory()
        handler = node_error_handler(store=_hitl_store(factory))
        cmd = _run(
            handler(
                {"messages": []},
                error=NodeError(node="execute_task", error=RuntimeError("no thread")),
                config={"configurable": {}},
            )
        )
        assert cmd.goto == END
        assert len(_rows(factory)) == 1


class TestStoreNotAssembled:
    """``store=None``（``HITL_PENDING_PERSIST_ENABLED`` 关或装配失败）不得兜底建表。

    库面装配判据唯一源是 ``hitl_bridge._resolve_pending_store``；handler 若自己
    兜底建表，flag 关时仍会建表写行（约定 6 ④ 的第二装配源）。
    """

    def test_none_store_skips_write_and_still_returns_failure_exit(self, monkeypatch):
        store_module = importlib.import_module(
            "agentix.infrastructure.persistence.hitl_pending_store"
        )
        touched: List[str] = []

        def _forbidden_builder(*args: Any, **kwargs: Any):
            touched.append("built")
            raise AssertionError("store=None 时 handler 不得兜底建表")

        monkeypatch.setattr(
            store_module, "build_default_hitl_pending_store", _forbidden_builder
        )
        # 即便 flag 开着，handler 也无权自行装配（判据不在这里）
        monkeypatch.setattr(settings, "HITL_PENDING_PERSIST_ENABLED", True)
        records, handler_id = _capture_logs()
        try:
            cmd = _invoke_handler(
                node_error_handler(store=None),
                node="execute_task",
                exc=RuntimeError("boom"),
            )
        finally:
            logger.remove(handler_id)

        assert isinstance(cmd, Command)
        assert cmd.goto == END
        content = cmd.update["messages"][0].content
        assert "本轮未能完成" in content
        assert "execute_task" in content and "RuntimeError" in content
        assert "已记录补偿项" not in content, "没写记录就不能声称已记录"
        assert any("compensation store 未装配" in r for r in records)
        assert touched == [], "handler 触达了 store 装配点"
        assert "build_default_hitl_pending_store" not in inspect.getsource(
            policies
        ), "policies 里不应再有兜底建表符号"


# ---------------------------------------------------------------------------
# 3) 框架语义：重试耗尽 → handler；GraphBubbleUp 不接管
# ---------------------------------------------------------------------------


def _compensation_graph(handler, *, attempts: int, retry: bool):
    calls = {"node": 0, "handler": 0}

    async def flaky(state: _State) -> Dict[str, Any]:
        calls["node"] += 1
        raise RuntimeError("flaky boom")

    async def tail(state: _State) -> Dict[str, Any]:
        calls["handler"] += 1
        return {"attempts": calls["node"]}

    builder = StateGraph(_State)
    retry_kwargs = (
        {
            "retry_policy": RetryPolicy(
                max_attempts=attempts,
                initial_interval=0.01,
                jitter=False,
                retry_on=lambda exc: True,
            )
        }
        if retry
        else {}
    )
    builder.add_node("flaky", flaky, error_handler=handler, **retry_kwargs)
    builder.add_node("tail", tail)
    builder.add_edge(START, "flaky")
    compiled = builder.compile()
    return compiled, calls


class TestFrameworkSemantics:
    def test_handler_runs_after_retries_are_exhausted(self):
        factory = _sqlite_factory()
        handler = node_error_handler(store=_hitl_store(factory), exit_node="tail")
        compiled, calls = _compensation_graph(handler, attempts=3, retry=True)
        out = _run(compiled.ainvoke({}, {"configurable": {"thread_id": "t-retry"}}))
        assert calls["node"] == 3, "retry_policy 的尝试次数必须先被耗尽"
        assert calls["handler"] == 1
        assert out["attempts"] == 3
        assert len(_rows(factory)) == 1

    def test_handler_runs_on_first_failure_without_retry_policy(self):
        factory = _sqlite_factory()
        handler = node_error_handler(store=_hitl_store(factory), exit_node="tail")
        compiled, calls = _compensation_graph(handler, attempts=3, retry=False)
        out = _run(compiled.ainvoke({}, {"configurable": {"thread_id": "t-norety"}}))
        assert calls["node"] == 1, "无 retry_policy 时「重试耗尽」即首次失败"
        assert calls["handler"] == 1
        assert out["attempts"] == 1

    def test_graph_bubble_up_is_not_routed_to_handler(self):
        """``interrupt`` 同族的 ``GraphBubbleUp`` 必须原样穿透（HITL 挂起语义不变）。"""
        factory = _sqlite_factory()
        handler = node_error_handler(store=_hitl_store(factory))

        class _Suspend(GraphBubbleUp):
            pass

        async def suspending(state: _State) -> Dict[str, Any]:
            raise _Suspend("interrupt-like")

        builder = StateGraph(_State)
        builder.add_node("suspending", suspending, error_handler=handler)
        builder.add_edge(START, "suspending")
        compiled = builder.compile()
        with pytest.raises(_Suspend):
            _run(compiled.ainvoke({}, {"configurable": {"thread_id": "t-bubble"}}))
        assert _rows(factory) == []


# ---------------------------------------------------------------------------
# 4) 主图接线：关时零注册、开时只挂两个有副作用节点
# ---------------------------------------------------------------------------


class TestMainGraphWiring:
    def test_off_registers_no_handler_node_at_all(self):
        compiled = lg_builder.build_graph(None)
        assert getattr(compiled, "node_error_handler_map", {}) == {}
        assert not [n for n in compiled.get_graph().nodes if "error_handler" in n]

    def test_helper_returns_empty_dict_when_off(self):
        assert lg_builder._node_error_handler_kwargs() == {}

    def test_on_attaches_only_to_side_effecting_nodes(self, monkeypatch):
        monkeypatch.setattr(settings, "NODE_ERROR_HANDLER_ENABLED", True)
        compiled = lg_builder.build_graph(None)
        mapping = dict(getattr(compiled, "node_error_handler_map", {}))
        assert mapping == {
            "execute_task": "__error_handler__execute_task",
            "code_agent": "__error_handler__code_agent",
        }
        assert set(mapping) == set(SIDE_EFFECT_NODES)
        for name in SIDE_EFFECT_NODES:
            assert compiled.nodes[name].error_handler_node == mapping[name]
            assert f"__error_handler__{name}" in compiled.get_graph().nodes

    def test_on_never_adds_retry_policy_to_side_effecting_nodes(self, monkeypatch):
        """补偿不等于重试：两个有副作用节点开关打开后仍必须无 retry_policy。"""
        monkeypatch.setattr(settings, "NODE_ERROR_HANDLER_ENABLED", True)
        compiled = lg_builder.build_graph(None)
        for name in SIDE_EFFECT_NODES:
            assert (
                compiled.nodes[name].retry_policy is None
            ), f"{name} 出现了 retry_policy——整节点重跑会重放工具副作用"

    def test_on_leaves_readonly_nodes_untouched(self, monkeypatch):
        monkeypatch.setattr(settings, "NODE_ERROR_HANDLER_ENABLED", True)
        compiled = lg_builder.build_graph(None)
        for name in (
            "message_window",
            "analyze_and_route_query",
            "respond_to_general_query",
            "get_additional_info",
            "create_research_plan",
            "create_image_query",
            "create_file_query",
            "create_kb_query",
        ):
            assert compiled.nodes[name].error_handler_node is None


# ---------------------------------------------------------------------------
# 5) 静态守卫：handler 实参必须来自 policies（p147 对 ** 展开豁免，这里补钉）
# ---------------------------------------------------------------------------


def _function_source(name: str) -> ast.FunctionDef:
    tree = ast.parse(LG_BUILDER_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"lg_builder 里没有 {name}")


class TestStaticGuard:
    def test_helper_references_policies_factory(self):
        fn = _function_source("_node_error_handler_kwargs")
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        assert "node_error_handler" in names, (
            "error_handler 必须由 policies.node_error_handler 构造（LG-0-3 集中点）；"
            "p147 对 ** 展开豁免，所以这里补一条按实参来源的绊线"
        )

    def test_helper_passes_store_from_hitl_bridge_single_source(self):
        """``store=`` 实参必须来自 ``hitl_bridge.resolve_pending_store()``。

        这条接缝是「补偿记录到底写不写」的唯一装配判据（store 为 ``None`` 时
        handler 只出「补偿记录未写（补偿存储未装配）」回执，一条记录都不写）。别的用例都
        显式注入 store，``store=`` 被删掉或改成 ``None`` 时它们仍然全绿，而生产上
        补偿记录永久为零——所以按 AST 钉实参来源，而不是断言 handler 挂没挂上。
        """
        fn = _function_source("_node_error_handler_kwargs")
        imported = {
            alias.name
            for node in ast.walk(fn)
            if isinstance(node, ast.ImportFrom)
            and node.module == "agentix.policies.hitl.hitl_bridge"
            for alias in node.names
        }
        assert "resolve_pending_store" in imported, (
            "lg_builder 必须从 agentix.policies.hitl.hitl_bridge 导入 resolve_pending_store："
            "补偿存储的装配判据只有一个源（约定 6 ④），另立判据或兜底建表都会漂移"
        )

        store_values = [
            kw.value
            for call in ast.walk(fn)
            if isinstance(call, ast.Call)
            and (
                (isinstance(call.func, ast.Name) and call.func.id == "node_error_handler")
                or (
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr == "node_error_handler"
                )
            )
            for kw in call.keywords
            if kw.arg == "store"
        ]
        assert store_values, (
            "node_error_handler(...) 必须显式传 store=；不传 ⇒ 补偿存储恒为未装配，"
            "生产上补偿记录一条都不会写"
        )
        # H11：判据取的是 store_values[0]，若出现多处 store= 调用则只看首个、
        # 第二处的错误装配会被静默放过。这里钉「唯一」，把「取首元素」的盲区补上；
        # 不改成遍历全部（会重写既有断言语义，属改判据而非只增，本批不取）。
        assert len(store_values) == 1, f"store= 判据必须唯一，实际 {len(store_values)} 处调用"
        value = store_values[0]
        assert (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "resolve_pending_store"
            and not value.args
            and not value.keywords
        ), (
            "store 实参必须是 resolve_pending_store() 的返回值（不另判 flag、不兜底建表）；"
            f"实际是 {ast.dump(value)[:120]}"
        )

    def test_handler_kwargs_expanded_at_side_effecting_nodes_only(self):
        """``**node_error_handler_kwargs`` 只出现在两个有副作用节点的 add_node 上。"""
        tree = ast.parse(LG_BUILDER_PATH.read_text(encoding="utf-8"))
        targets: List[str] = []
        for call in ast.walk(tree):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "add_node"
            ):
                continue
            expanded = any(
                kw.arg is None
                and isinstance(kw.value, ast.Name)
                and kw.value.id == "node_error_handler_kwargs"
                for kw in call.keywords
            )
            if not expanded:
                continue
            first = call.args[0] if call.args else None
            if isinstance(first, ast.Constant):
                targets.append(str(first.value))
            else:
                targets.append("<dynamic>")
        assert sorted(targets) == sorted(
            SIDE_EFFECT_NODES
        ), f"error_handler 只允许挂在 {SIDE_EFFECT_NODES}，实际：{targets}"
