"""Stage F1e · A-P1-4：HITL 进环（统一执行链 Stage F）。

三子句 + flag 治理（默认 ``HITL_IN_LOOP_ENABLED=False`` 与基线逐位等价）：

- **① 进环挂起/恢复**：lg_builder 注入壳四条件门控（py3.12+ / 本 flag /
  ``HUMAN_REVIEW_ENABLED`` / 非 stream ask 声明的进环意图）把
  ``langgraph.types.interrupt`` 经 config 递给 ``execute_task``；
  ``task_execution`` 消费 ``review_required``——``approve`` 落批准后重试
  一次并把该步 outcome 换成成功，``reject``/未知值 fail-closed 快速失败
  （终止后续波次回 failed 终态，保持 run_task_execution 不抛契约）。
  GraphInterrupt 必须在 ``run_batch`` 之外抛出（``run_task`` 会吞）。
- **② 无 approver REJECT**：闸开 + 无 approver + 无决策面 ⇒
  ``reason=no_approver_wired_fail_closed``（沿 sidecar 语义，不留无人批
  的 PENDING）；有决策面（HITL 桥）或 flag-off ⇒ 照旧 review_required。
- **③ 裁决顺序** deny→hook→ask强制→allow 契约锁定（source-code-study
  Codex 序；hook 可代批但代批仍须再过 ask，allow 走执行点消费）。

py3.10：async ``interrupt`` 必死锁（test_p32 双向锁特征化）⇒ 注入壳
版本门控，3.10 诚实降级=不注入；真正挂起恢复的 e2e 标记 skipif py3.12+。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from agentix.config.settings import settings
from agentix.application.services.artifact_store import (
    get_artifact_store,
    reset_artifact_store,
)
from agentix.application.services.task_execution import run_task_execution
from agentix.application.services.task_tools_bridge import install_task_write_tools
from agentix.application.agents.task_handler import execute_task
from agentix.tools import registry_init
from agentix.tools.definitions import ToolSpec, registry as tool_registry
from agentix.tools.gateway.executor import get_tool_executor, reset_tool_executor
from agentix.tools.gateway.human_review import (
    ReviewRequest,
    clear_decision_surface,
    get_review_gate,
    mark_decision_surface,
    reset_review_gate,
)

REPO = Path(__file__).resolve().parents[1]

_TASK_TOOLS = ("append_task_note", "write_task_artifact", "read_artifact")


def _reset_task_decls() -> None:
    registry_init._DECLARATIONS.clear()
    for name in _TASK_TOOLS:
        tool_registry.delete_tool(name)


def _reset_all() -> None:
    _reset_task_decls()
    reset_tool_executor()
    reset_artifact_store()
    reset_review_gate()
    clear_decision_surface()
    tool_registry.delete_tool("p81_probe")


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def gate_env(monkeypatch: pytest.MonkeyPatch):
    """闸开（HUMAN_REVIEW）+ 单例复位；HITL flag 由各测试自定。"""
    monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True)
    reset_review_gate()
    reset_tool_executor()
    yield
    _reset_all()


@pytest.fixture
def loop_env(monkeypatch: pytest.MonkeyPatch):
    """任务管线 + 闸开（HUMAN_REVIEW）+ 桥决策面（进环挂起的生产前置）。

    决策面置位 ⇒ Q3 的「无通道 REJECT」不触发，reason 落 review_required
    （正是 interrupt 挂起的入口）；无通道 REJECT 语义由 TestNoApproverReject 单测。
    """
    monkeypatch.setattr(settings, "TASK_EXEC_ENABLED", True)
    monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True)
    _reset_all()
    mark_decision_surface()
    install_task_write_tools()
    yield
    _reset_all()


def _register_probe(risk: str = "L2", handler=None):
    """注册一个可控探针工具（默认 L2 = 进闸高风险；L1+ 须带 control 施措）。"""
    spec_kwargs: Dict[str, Any] = {}
    if risk != "L0":
        spec_kwargs["control"] = "人工审批（HITL）后方可执行"
    spec = ToolSpec(
        name="p81_probe",
        risk_level=risk,
        description="p81 in-loop probe",
        parameters={},
        **spec_kwargs,
    )
    tool_registry.register(spec, lambda: None)
    calls: List[Dict[str, Any]] = []

    async def _default(args: dict) -> dict:
        calls.append(dict(args))
        return {"ok": True}

    ex = get_tool_executor()
    ex.register_handler("p81_probe", handler or _default, spec=spec)
    return ex, calls


def _write_steps(name: str = "p81.md") -> list:
    return [
        {
            "id": "s1",
            "tool": "write_task_artifact",
            "args": {"name": name, "content": "F1e 正文", "kind": "report"},
            "depends_on": [],
            "rationale": "高风险写（进环探针）",
        }
    ]


def _stats() -> dict:
    return get_review_gate().stats()


def _by_decision(kind: str) -> int:
    return int(_stats().get("by_decision", {}).get(kind, 0))


# ---------------------------------------------------------------------------
# 1. 五处同步 + flag 默认治理
# ---------------------------------------------------------------------------


class TestFlagSync:
    def test_default_flag_off(self):
        assert settings.HITL_IN_LOOP_ENABLED is False

    def test_field_declared_as_bool(self):
        fields = type(settings).model_fields  # 类访问（实例 model_fields 已弃用）
        assert "HITL_IN_LOOP_ENABLED" in fields
        assert fields["HITL_IN_LOOP_ENABLED"].default is False

    def test_env_example_documents_key(self):
        text = (REPO / ".env.example").read_text(encoding="utf-8")
        assert "HITL_IN_LOOP_ENABLED=false" in text

    def test_configuration_doc_documents_key(self):
        text = (REPO / "docs/architecture/configuration.md").read_text(encoding="utf-8")
        assert "| `HITL_IN_LOOP_ENABLED` | `false`" in text
        assert "`HITL_IN_LOOP_ENABLED`" in text  # §2 精选键行

    def test_selfcheck_allows_omission(self):
        from agentix.config.selfcheck import DEFAULT_IGNORED_MISSING

        assert "HITL_IN_LOOP_ENABLED" in DEFAULT_IGNORED_MISSING

    def test_p41_key_table_lists_key(self):
        text = (REPO / "tests/test_p41_doc_consistency.py").read_text(encoding="utf-8")
        assert '"HITL_IN_LOOP_ENABLED"' in text


# ---------------------------------------------------------------------------
# 2. 注入壳门控（py3.12+/flag/父闸/进环意图；flag-off 透传原 config）
# ---------------------------------------------------------------------------


class TestInjectionGate:
    @staticmethod
    async def _fake_node(state, *, config):
        return {"config": config}

    def _cfg(self, intent: bool = True) -> dict:
        configurable: Dict[str, Any] = {"thread_id": "p81-t"}
        if intent:
            configurable["hitl_in_loop"] = 1
        return {"configurable": configurable}

    @pytest.mark.asyncio
    async def test_flag_on_py312_intent_injects_interrupt(self, monkeypatch):
        import agentix.orchestration.langgraph.lg_builder as lg

        monkeypatch.setattr(settings, "HITL_IN_LOOP_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True)
        monkeypatch.setattr(sys, "version_info", (3, 12, 0))
        cfg = self._cfg()
        out = await lg._wrap_execute_task_in_loop(self._fake_node)({}, config=cfg)
        injected = out["config"]["configurable"].get("hitl_in_loop_interrupt")
        assert injected is not None and callable(injected)
        # 原 config 不被就地篡改（防跨节点污染）
        assert "hitl_in_loop_interrupt" not in cfg["configurable"]

    @pytest.mark.asyncio
    async def test_flag_off_passes_config_through(self, monkeypatch):
        import agentix.orchestration.langgraph.lg_builder as lg

        monkeypatch.setattr(settings, "HITL_IN_LOOP_ENABLED", False)
        monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True)
        monkeypatch.setattr(sys, "version_info", (3, 12, 0))
        cfg = self._cfg()
        out = await lg._wrap_execute_task_in_loop(self._fake_node)({}, config=cfg)
        # flag-off：同一对象透传 = 逐位等价
        assert out["config"] is cfg
        assert "hitl_in_loop_interrupt" not in cfg["configurable"]

    @pytest.mark.asyncio
    async def test_no_intent_no_injection(self, monkeypatch):
        import agentix.orchestration.langgraph.lg_builder as lg

        monkeypatch.setattr(settings, "HITL_IN_LOOP_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True)
        monkeypatch.setattr(sys, "version_info", (3, 12, 0))
        cfg = self._cfg(intent=False)
        out = await lg._wrap_execute_task_in_loop(self._fake_node)({}, config=cfg)
        assert out["config"] is cfg  # stream 恒 L2：无意图 = 不注入不挂起

    @pytest.mark.asyncio
    async def test_parent_gate_off_no_injection(self, monkeypatch):
        import agentix.orchestration.langgraph.lg_builder as lg

        monkeypatch.setattr(settings, "HITL_IN_LOOP_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", False)
        monkeypatch.setattr(sys, "version_info", (3, 12, 0))
        out = await lg._wrap_execute_task_in_loop(self._fake_node)(
            {}, config=self._cfg()
        )
        assert "hitl_in_loop_interrupt" not in out["config"]["configurable"]

    @pytest.mark.asyncio
    async def test_real_version_gate(self, monkeypatch):
        """按真实解释器断言：3.10 降级不注入；3.12+ 注入（双分支特征化）。"""
        import agentix.orchestration.langgraph.lg_builder as lg

        monkeypatch.setattr(settings, "HITL_IN_LOOP_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True)
        out = await lg._wrap_execute_task_in_loop(self._fake_node)(
            {}, config=self._cfg()
        )
        injected = "hitl_in_loop_interrupt" in out["config"]["configurable"]
        assert injected is (sys.version_info >= (3, 12))

    @pytest.mark.asyncio
    async def test_node_result_untouched(self, monkeypatch):
        import agentix.orchestration.langgraph.lg_builder as lg

        monkeypatch.setattr(settings, "HITL_IN_LOOP_ENABLED", False)
        out = await lg._wrap_execute_task_in_loop(self._fake_node)(
            {"x": 1}, config=self._cfg()
        )
        assert out == {"config": {"configurable": {"thread_id": "p81-t",
                                                   "hitl_in_loop": 1}}}


# ---------------------------------------------------------------------------
# 3. ② 无 approver REJECT（flag-on）/ flag-off 与基线逐位等价
# ---------------------------------------------------------------------------


class TestNoApproverReject:
    @pytest.mark.asyncio
    async def test_flag_on_rejects_without_approver_or_surface(
        self, gate_env, monkeypatch
    ):
        monkeypatch.setattr(settings, "HITL_IN_LOOP_ENABLED", True)
        ex, calls = _register_probe()
        result = await ex.execute("p81_probe", {"x": 1})
        assert not result.success
        assert result.metadata.get("reason") == "no_approver_wired_fail_closed"
        assert result.metadata.get("request_id")
        assert calls == []  # 审批前 handler 绝不执行
        assert _by_decision("rejected") == 1
        assert _by_decision("pending") == 0  # 不留无人批的 PENDING

    @pytest.mark.asyncio
    async def test_flag_on_with_decision_surface_keeps_review_required(
        self, gate_env, monkeypatch
    ):
        monkeypatch.setattr(settings, "HITL_IN_LOOP_ENABLED", True)
        mark_decision_surface()
        ex, _ = _register_probe()
        result = await ex.execute("p81_probe", {"x": 1})
        assert not result.success
        assert result.metadata.get("reason") == "review_required"
        assert _by_decision("pending") == 1  # 桥决策面在 ⇒ 照旧待批
        assert _by_decision("rejected") == 0

    @pytest.mark.asyncio
    async def test_flag_off_baseline_byte_equal(self, gate_env, monkeypatch):
        """flag-off：无 approver + 无决策面 ⇒ 仍是 review_required（PENDING）。"""
        monkeypatch.setattr(settings, "HITL_IN_LOOP_ENABLED", False)
        ex, calls = _register_probe()
        result = await ex.execute("p81_probe", {"x": 1})
        assert not result.success
        assert result.metadata.get("reason") == "review_required"
        assert result.metadata.get("risk_level")
        assert calls == []
        assert _by_decision("pending") == 1
        assert _by_decision("rejected") == 0

    @pytest.mark.asyncio
    async def test_flag_on_safe_tool_unaffected(self, gate_env, monkeypatch):
        monkeypatch.setattr(settings, "HITL_IN_LOOP_ENABLED", True)
        ex, calls = _register_probe(risk="L0")
        result = await ex.execute("p81_probe", {"x": 1})
        assert result.success
        assert calls == [{"x": 1}]
        assert _stats().get("total", 0) == 0  # 低风险零登记

    @pytest.mark.asyncio
    async def test_flag_on_reject_is_sticky_per_call(self, gate_env, monkeypatch):
        """每次调用各自否决：连续两次 = 两条 REJECT、始终无 PENDING 泄漏。"""
        monkeypatch.setattr(settings, "HITL_IN_LOOP_ENABLED", True)
        ex, _ = _register_probe()
        await ex.execute("p81_probe", {})
        await ex.execute("p81_probe", {})
        assert _by_decision("rejected") == 2
        assert _by_decision("pending") == 0


# ---------------------------------------------------------------------------
# 4. ③ 裁决顺序契约：deny → hook → ask强制 → allow（Codex 序）
# ---------------------------------------------------------------------------


class TestOrderContract:
    @pytest.mark.asyncio
    async def test_hook_receives_materialized_request_flag_on(
        self, gate_env, monkeypatch
    ):
        monkeypatch.setattr(settings, "HITL_IN_LOOP_ENABLED", True)
        mark_decision_surface()
        seen: List[Any] = []

        async def _hook(request, raw_args):
            seen.append((request, dict(raw_args)))
            return None  # 不代批

        ex, _ = _register_probe()
        ex.set_review_blocked_hook(_hook)
        try:
            result = await ex.execute("p81_probe", {"x": 42})
        finally:
            ex.set_review_blocked_hook(None)
        assert not result.success
        assert result.metadata.get("reason") == "review_required"
        assert len(seen) == 1
        request, raw_args = seen[0]
        assert isinstance(request, ReviewRequest)
        assert request.request_id == result.metadata.get("request_id")
        assert raw_args == {"x": 42}

    @pytest.mark.asyncio
    async def test_hook_approval_still_forces_ask(self, gate_env, monkeypatch):
        """hook 代批不豁免 ask：无批准记录 ⇒ 仍 fail-closed review_required。"""
        monkeypatch.setattr(settings, "HITL_IN_LOOP_ENABLED", True)
        mark_decision_surface()
        hook_calls: List[int] = []

        async def _hook(request, raw_args):
            hook_calls.append(1)
            return True  # 代批（但闸门无记录）

        ex, _ = _register_probe()
        ex.set_review_blocked_hook(_hook)
        try:
            result = await ex.execute("p81_probe", {"x": 1})
        finally:
            ex.set_review_blocked_hook(None)
        assert not result.success
        assert result.metadata.get("reason") == "review_required"
        assert hook_calls == [1]
        # ask 物化的请求仍在待批面（hook 代批不豁免闸；基线行为：首闸+复闸
        # 各自登记一条 PENDING，故此处断言「终局那条在」而非计数）。
        pending_ids = {r.request_id for r in get_review_gate().pending()}
        assert result.metadata.get("request_id") in pending_ids

    @pytest.mark.asyncio
    async def test_flag_off_hook_path_unchanged(self, gate_env, monkeypatch):
        """flag-off：原顺序（ask 物化 → hook → 复闸）行为逐位不变。"""
        monkeypatch.setattr(settings, "HITL_IN_LOOP_ENABLED", False)
        mark_decision_surface()
        seen: List[Any] = []

        async def _hook(request, raw_args):
            seen.append(request.request_id)
            return None

        ex, _ = _register_probe()
        ex.set_review_blocked_hook(_hook)
        try:
            result = await ex.execute("p81_probe", {"x": 7})
        finally:
            ex.set_review_blocked_hook(None)
        assert result.metadata.get("reason") == "review_required"
        assert seen == [result.metadata.get("request_id")]


# ---------------------------------------------------------------------------
# 5. ① 进环挂起/恢复（fake interrupt：approve 落批准重试 / reject 快速失败）
# ---------------------------------------------------------------------------


class TestInLoopPipeline:
    @pytest.mark.asyncio
    async def test_approve_resumes_and_completes(self, loop_env, monkeypatch):
        monkeypatch.setattr(settings, "HITL_IN_LOOP_ENABLED", True)
        payloads: List[dict] = []

        def _interrupt(payload: dict) -> str:
            payloads.append(payload)
            return "approve"

        result = await run_task_execution(
            "产出报告",
            session_id="p81-approve",
            task_steps=_write_steps(),
            review_interrupt=_interrupt,
        )
        assert result.terminal == "completed"
        assert result.passed is True
        assert len(payloads) == 1
        payload = payloads[0]
        assert payload["tool"] == "write_task_artifact"
        assert payload["request_id"]
        assert payload["risk_level"] == "high"
        assert payload["session_id"] == "p81-approve"
        assert "spec" not in payload  # 传图的载荷只含纯数据（可序列化）
        assert _by_decision("approved") == 1  # resume 值落成批准
        store = get_artifact_store()
        assert len(store._items) == 1  # 审批后重试恰执行一次

    @pytest.mark.asyncio
    async def test_reject_aborts_fast_without_side_effects(
        self, loop_env, monkeypatch
    ):
        monkeypatch.setattr(settings, "HITL_IN_LOOP_ENABLED", True)

        def _interrupt(payload: dict) -> str:
            return "reject"

        result = await run_task_execution(
            "产出报告（应被拒）",
            session_id="p81-reject",
            task_steps=_write_steps(),
            review_interrupt=_interrupt,
        )
        assert result.terminal == "failed"
        assert result.passed is False
        assert "in_loop_review_rejected" in result.reason
        assert _by_decision("rejected") == 1  # 拒绝落账（不留 PENDING）
        assert len(get_artifact_store()._items) == 0  # 无副作用

    @pytest.mark.asyncio
    async def test_unknown_decision_fails_closed(self, loop_env, monkeypatch):
        monkeypatch.setattr(settings, "HITL_IN_LOOP_ENABLED", True)

        def _interrupt(payload: dict) -> str:
            return "maybe"

        result = await run_task_execution(
            "产出报告（未知恢复值）",
            session_id="p81-unknown",
            task_steps=_write_steps(),
            review_interrupt=_interrupt,
        )
        assert result.terminal == "failed"
        assert "bad resume decision" in result.reason
        assert _by_decision("pending") == 1  # 未知值不落判定（仍待批，fail-closed）
        assert len(get_artifact_store()._items) == 0

    @pytest.mark.asyncio
    async def test_non_review_failure_never_calls_interrupt(
        self, loop_env, monkeypatch
    ):
        monkeypatch.setattr(settings, "HITL_IN_LOOP_ENABLED", True)

        spec = ToolSpec(
            name="p81_probe",
            risk_level="L0",
            description="fails without review",
            parameters={},
        )
        tool_registry.register(spec, lambda: None)

        async def _boom(args: dict) -> dict:
            raise ValueError("invalid_request 400: nope")

        get_tool_executor().register_handler("p81_probe", _boom, spec=spec)
        calls: List[dict] = []

        def _interrupt(payload: dict) -> str:
            calls.append(payload)
            return "approve"

        result = await run_task_execution(
            "必败任务",
            session_id="p81-boom",
            task_steps=[
                {"id": "s1", "tool": "p81_probe", "args": {"x": 1}}
            ],
            review_interrupt=_interrupt,
        )
        assert result.terminal in ("failed", "verify_failed")
        assert calls == []  # 非 review_required 不挂起

    @pytest.mark.asyncio
    async def test_interrupt_none_keeps_baseline_semantics(self, loop_env):
        """review_interrupt 缺省 = 现行为：verify_failed + 请求留 PENDING。"""
        result = await run_task_execution(
            "产出报告（基线路径）",
            session_id="p81-baseline",
            task_steps=_write_steps(),
        )
        assert result.terminal == "verify_failed"
        assert _by_decision("pending") == 1
        assert len(get_artifact_store()._items) == 0

    @pytest.mark.asyncio
    async def test_dependent_second_step_interrupts_again(
        self, loop_env, monkeypatch
    ):
        """链式两步各自进闸：每步一次 interrupt，全部 approve ⇒ 完成。"""
        monkeypatch.setattr(settings, "HITL_IN_LOOP_ENABLED", True)
        steps = _write_steps() + [
            {
                "id": "s2",
                "tool": "append_task_note",
                "args": {"note": "收尾", "key": "p81"},
                "depends_on": ["s1"],
                "rationale": "追加笔记（medium 也进闸）",
            }
        ]
        payloads: List[dict] = []

        def _interrupt(payload: dict) -> str:
            payloads.append(payload)
            return "approve"

        result = await run_task_execution(
            "写产物再记笔记",
            session_id="p81-chain",
            task_steps=steps,
            review_interrupt=_interrupt,
        )
        assert result.terminal == "completed"
        assert len(payloads) == 2
        assert [p["tool"] for p in payloads] == [
            "write_task_artifact",
            "append_task_note",
        ]
        assert _by_decision("approved") == 2
        assert len(get_artifact_store()._items) == 2

    @pytest.mark.asyncio
    async def test_execute_task_node_passes_interrupt_through(
        self, loop_env, monkeypatch
    ):
        """图节点链：config 里的中断回调贯通到 run_task_execution。"""
        monkeypatch.setattr(settings, "HITL_IN_LOOP_ENABLED", True)
        payloads: List[dict] = []

        def _interrupt(payload: dict) -> str:
            payloads.append(payload)
            return "approve"

        from agentix.orchestration.langgraph.lg_states import AgentState
        from langchain_core.messages import HumanMessage

        state = AgentState(messages=[HumanMessage(content="产出报告")])
        out = await execute_task(
            state,
            config={
                "configurable": {
                    "thread_id": "p81-node",
                    "task_steps": _write_steps(),
                    "hitl_in_loop_interrupt": _interrupt,
                }
            },
        )
        assert out.get("messages")
        assert len(payloads) == 1
        assert payloads[0]["session_id"] == "p81-node"
        assert len(get_artifact_store()._items) == 1


# ---------------------------------------------------------------------------
# 6. ops 恢复面 + 接线锁 + 真图 e2e（py3.12+）
# ---------------------------------------------------------------------------


class TestOpsResumeEndpoint:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["approve", "reject"])
    async def test_resume_delegates_to_lg_helper(self, monkeypatch, value):
        import agentix.orchestration.langgraph.lg_builder as lg
        from agentix.interfaces.http.ops_router import (
            InLoopResumeRequest,
            resume_in_loop_hitl,
        )

        seen: dict = {}

        async def _fake_resume(thread_id, resume_value):
            seen["thread_id"] = thread_id
            seen["resume_value"] = resume_value
            return {"messages": ["ok"]}

        monkeypatch.setattr(lg, "resume_in_loop", _fake_resume)
        out = await resume_in_loop_hitl(
            InLoopResumeRequest(thread_id="t-1", resume_value=value)
        )
        assert seen == {"thread_id": "t-1", "resume_value": value}
        assert out["status"] == "resumed"
        assert out["message_count"] == 1

    @pytest.mark.asyncio
    async def test_invalid_resume_value_400(self):
        from fastapi import HTTPException

        from agentix.interfaces.http.ops_router import (
            InLoopResumeRequest,
            resume_in_loop_hitl,
        )

        with pytest.raises(HTTPException) as ei:
            await resume_in_loop_hitl(
                InLoopResumeRequest(thread_id="t", resume_value="abort")
            )
        assert ei.value.status_code == 400

    @pytest.mark.asyncio
    async def test_no_pending_thread_409(self, monkeypatch):
        import agentix.orchestration.langgraph.lg_builder as lg
        from fastapi import HTTPException

        from agentix.interfaces.http.ops_router import (
            InLoopResumeRequest,
            resume_in_loop_hitl,
        )

        async def _boom(thread_id, resume_value):
            raise RuntimeError("no pending interrupt")

        monkeypatch.setattr(lg, "resume_in_loop", _boom)
        with pytest.raises(HTTPException) as ei:
            await resume_in_loop_hitl(
                InLoopResumeRequest(thread_id="ghost", resume_value="approve")
            )
        assert ei.value.status_code == 409


class TestWiringLocks:
    def test_chat_service_declares_in_loop_intent(self):
        text = (REPO / "agentix/application/services/chat_service.py").read_text(
            encoding="utf-8"
        )
        assert 'config["configurable"]["hitl_in_loop"] = 1' in text

    def test_ops_router_has_no_top_level_langgraph_import(self):
        """p66 纪律：langgraph 依赖留在 lg_builder，路由只做惰性调用。"""
        src = (REPO / "agentix/interfaces/http/ops_router.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, ast.Import):
                assert all(
                    not (alias.name == "langgraph" or alias.name.startswith("langgraph."))
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("langgraph")

    def test_lg_builder_wraps_and_resumes(self):
        text = (REPO / "agentix/orchestration/langgraph/lg_builder.py").read_text(
            encoding="utf-8"
        )
        assert "_wrap_execute_task_in_loop(execute_task)" in text
        assert "def resume_in_loop(" in text
        assert "hitl_in_loop_interrupt" in text


@pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="async langgraph interrupt 需 py3.12+（py3.10 死锁见 test_p32 特征化）",
)
class TestRealGraphE2E:
    @pytest.mark.asyncio
    async def test_wrapper_suspends_and_resumes_via_command(self, monkeypatch):
        """真图：注入壳 → interrupt 挂起 → Command(resume) 恢复返回裁决值。"""
        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.graph import START, StateGraph
        from langgraph.types import Command

        import agentix.orchestration.langgraph.lg_builder as lg
        from agentix.orchestration.langgraph.lg_states import AgentState, InputState

        monkeypatch.setattr(settings, "HITL_IN_LOOP_ENABLED", True)
        monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True)

        captured: Dict[str, Any] = {}

        async def _probe(state, *, config):
            fn = (config.get("configurable") or {}).get("hitl_in_loop_interrupt")
            captured["injected"] = fn is not None
            if fn is None:
                captured["decision"] = "no_interrupt"
            else:
                captured["decision"] = fn({"tool": "probe", "request_id": "r1"})
            # 图节点须至少写一个状态通道（AgentState 无通用 dummy 通道）
            return {"answer": str(captured["decision"])}

        builder = StateGraph(AgentState, input=InputState)
        builder.add_node("probe", lg._wrap_execute_task_in_loop(_probe))
        builder.add_edge(START, "probe")
        graph = builder.compile(checkpointer=MemorySaver())

        out1 = await graph.ainvoke(
            {"messages": [{"type": "human", "content": "e2e"}]},
            config={"configurable": {"thread_id": "p81-e2e", "hitl_in_loop": 1}},
        )
        assert captured["injected"] is True
        # 挂起：节点在 interrupt 处中止 ⇒ 决策未回填、无 answer 写入
        #（p32:445-446 同款特征化断言——本版本 ainvoke 只回通道状态）
        assert "decision" not in captured
        assert "answer" not in dict(out1)

        out2 = await graph.ainvoke(
            Command(resume="approve"),
            config={"configurable": {"thread_id": "p81-e2e", "hitl_in_loop": 1}},
        )
        assert captured["decision"] == "approve"  # 恢复：interrupt 返回裁决值
        assert dict(out2).get("answer") == "approve"  # 节点跑完并落状态
