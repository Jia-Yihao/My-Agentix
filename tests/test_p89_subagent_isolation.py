"""Stage F2c-b（统一执行链 · A-P2-1）：子代理隔离三件套契约守卫。

钉死契约：

1. **总闸默认关**：``MULTIAGENT_SUBAGENT_ISOLATION_ENABLED`` 默认 False ⇒
   supervisor 与基线逐位等价（handler 收原 state/config 对象、meta 无
   ``isolation`` 键、Manager 不收 ``budgets``）；
2. **三件套语义（flag-on）**：① ephemeral——handler 子 agent 只见任务面
   （messages 收敛到最后一条，父对话轨迹不进）；② skip_memory——config
   摘除活 ``memory_context``，唯一记忆入口 = ``MemorySnapshot`` 只读快照
   （``memory_snapshot`` 键；Mapping 深拷贝后 ``MappingProxyType`` 冻结，
   写入抛 TypeError）；③ 独立 session+budget——每 actor 一个
   ``SubAgentSession``（session_id + 独立 PeerBudget），经
   ``session_budgets`` 注入 Manager ``budgets`` 单源；
3. **控制面**：心跳保活（包装层被调即刷新，TTL 外 ``is_alive=False``）；
   kill ⇒ 闸短路返回 ``session=killed`` 且**内层不执行**；pause ⇒ 有界等待
   （resume 继续 / kill 短路 / 超时 fail-open 放行，绝不悬挂）；steer ⇒
   指令一次性注入下轮 context 并落 ``steered`` 标记；
4. **观测**：meta 仅 flag-on 追加 ``isolation``（policy/sessions/
   snapshot_chars/registry），flag-off 与基线键集逐位一致。
"""
from __future__ import annotations

import asyncio
import time
from types import MappingProxyType

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agentix.config import settings
from agentix.orchestration.langgraph import lg_supervisor
from agentix.orchestration.langgraph.lg_states import AgentState, Router
from agentix.orchestration.strategies.multi_agent import isolation as iso
from agentix.orchestration.strategies.multi_agent.manager import (
    FailureMode,
    ManagerOutcome,
    PeerBudget,
)
from agentix.orchestration.strategies.multi_agent.peer import AgentRole, PeerResult

FLAG = "MULTIAGENT_SUBAGENT_ISOLATION_ENABLED"


@pytest.fixture(autouse=True)
def _offline_no_llm(monkeypatch):
    monkeypatch.setattr(lg_supervisor, "_llm_available", lambda: False)


def _state() -> AgentState:
    state = AgentState(
        messages=[HumanMessage(content="旧问题"), HumanMessage(content="新问题")]
    )
    state.router = Router(type="general-query", logic="test")
    return state


class _RecordingHandler:
    def __init__(self, content: str = "这是子 agent 的草稿答案。"):
        self.content = content
        self.state = None
        self.config = None

    async def __call__(self, state, *, config=None):
        self.state = state
        self.config = config
        return {"messages": [AIMessage(content=self.content)], "sources": []}


def _session(actor: str = "proposer", **kw) -> iso.SubAgentSession:
    return iso.SubAgentSession(actor=actor, budget=PeerBudget(), **kw)


# ---------------------------------------------------------------------------
# 1. Policy / 只读快照
# ---------------------------------------------------------------------------


class TestPolicyAndSnapshot:
    def test_policy_defaults(self):
        pol = iso.IsolationPolicy()
        assert pol.ephemeral_prompt and pol.skip_memory
        assert pol.memory_snapshot_readonly
        assert pol.heartbeat_ttl_seconds > 0
        assert pol.max_pause_wait_seconds > 0
        assert set(pol.to_dict()) == {
            "ephemeral_prompt",
            "skip_memory",
            "memory_snapshot_readonly",
            "heartbeat_ttl_seconds",
            "max_pause_wait_seconds",
        }

    def test_snapshot_of_str(self):
        snap = iso.take_memory_snapshot("长期记忆片段：甲")
        assert isinstance(snap, iso.MemorySnapshot)
        assert snap.text == "长期记忆片段：甲"
        assert snap.chars == len("长期记忆片段：甲")
        assert snap.section() == "长期记忆片段：甲"
        assert snap.created_at > 0

    @pytest.mark.parametrize("empty", [None, "", {}])
    def test_snapshot_of_empty_is_none(self, empty):
        assert iso.take_memory_snapshot(empty) is None

    def test_snapshot_of_mapping_is_frozen(self):
        snap = iso.take_memory_snapshot({"k": "v"})
        assert isinstance(snap.data, MappingProxyType)
        with pytest.raises(TypeError):
            snap.data["k"] = "tampered"  # type: ignore[index]

    def test_snapshot_unsupported_type_is_none(self):
        assert iso.take_memory_snapshot(12345) is None

    def test_snapshot_dataclass_is_frozen(self):
        import dataclasses

        snap = iso.take_memory_snapshot("x")
        with pytest.raises(dataclasses.FrozenInstanceError):
            snap.text = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. 任务面 prompt（ephemeral 唯一构造点）
# ---------------------------------------------------------------------------


class TestTaskFacePrompt:
    def test_ephemeral_drops_parent_context(self):
        text = iso.build_task_face_prompt(
            "目标", blackboard="黑板", parent_context="父轨迹全文"
        )
        assert "目标" in text and "黑板" in text
        assert "父轨迹全文" not in text

    def test_non_ephemeral_includes_parent_context(self):
        pol = iso.IsolationPolicy(ephemeral_prompt=False)
        text = iso.build_task_face_prompt(
            "目标", parent_context="父轨迹全文", policy=pol
        )
        assert "父轨迹全文" in text

    def test_snapshot_section_gated_by_readonly_policy(self):
        snap = iso.take_memory_snapshot("记忆正文")
        with_snap = iso.build_task_face_prompt("目标", snapshot=snap)
        assert "【父记忆快照（只读）】" in with_snap and "记忆正文" in with_snap

        pol = iso.IsolationPolicy(memory_snapshot_readonly=False)
        without = iso.build_task_face_prompt("目标", snapshot=snap, policy=pol)
        assert "记忆正文" not in without


# ---------------------------------------------------------------------------
# 3. live registry（心跳 / kill / pause / steer）
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_register_and_lookup(self):
        reg = iso.LiveSubagentRegistry()
        session = reg.register(_session("proposer"))
        assert reg.get(session.session_id) is session
        assert reg.by_actor("proposer") is session
        assert reg.get("missing") is None
        assert session.state == iso.SESSION_ALIVE

    def test_heartbeat_ttl_window(self):
        reg = iso.LiveSubagentRegistry(policy=iso.IsolationPolicy(heartbeat_ttl_seconds=10))
        session = reg.register(_session())
        assert reg.is_alive(session.session_id) is True
        assert reg.is_alive(session.session_id, at=session.last_heartbeat + 11) is False
        assert reg.heartbeat(session.session_id, at=session.last_heartbeat + 20)
        assert reg.is_alive(session.session_id) is True

    def test_pause_resume_lifecycle(self):
        reg = iso.LiveSubagentRegistry()
        session = reg.register(_session())
        assert reg.pause(session.session_id) is True
        assert session.state == iso.SESSION_PAUSED
        assert reg.is_alive(session.session_id) is False  # paused ≠ alive
        assert reg.resume(session.session_id) is True
        assert session.state == iso.SESSION_ALIVE
        assert reg.resume(session.session_id) is False  # 已在 alive

    def test_kill_is_terminal_and_audited(self):
        reg = iso.LiveSubagentRegistry()
        session = reg.register(_session())
        assert reg.kill(session.session_id) is True
        assert reg.kill(session.session_id) is False  # 幂等
        assert session.state == iso.SESSION_KILLED
        assert reg.heartbeat(session.session_id) is False
        assert reg.resume(session.session_id) is False
        assert reg.pause(session.session_id) is False
        assert reg.steer(session.session_id, "x") is False
        status = reg.status()
        assert status["counts"]["killed"] == 1
        assert status["sessions"][0]["state"] == iso.SESSION_KILLED

    def test_steer_is_consume_once(self):
        reg = iso.LiveSubagentRegistry()
        session = reg.register(_session())
        assert reg.steer(session.session_id, "注意利息上限") is True
        assert reg.take_steering(session.session_id) == "注意利息上限"
        assert reg.take_steering(session.session_id) == ""

    def test_status_counts_and_fields(self):
        reg = iso.LiveSubagentRegistry()
        a = reg.register(_session("proposer"))
        b = reg.register(_session("reviewer"))
        reg.pause(b.session_id)
        reg.steer(a.session_id, "s")
        status = reg.status()
        assert status["counts"] == {"alive": 1, "paused": 1, "killed": 0}
        by_actor = {rec["actor"]: rec for rec in status["sessions"]}
        assert by_actor["proposer"]["steering_pending"] is True
        assert by_actor["reviewer"]["state"] == iso.SESSION_PAUSED
        assert by_actor["proposer"]["heartbeat_age"] >= 0
        assert "max_steps" in by_actor["proposer"]["budget"]

    def test_session_budgets_map(self):
        a, b = _session("proposer"), _session("reviewer")
        budgets = iso.session_budgets((a, b))
        assert budgets == {"proposer": a.budget, "reviewer": b.budget}
        assert budgets["proposer"] is a.budget

    def test_session_budget_independent_copy(self):
        """独立 budget：会话各持一份，互不共享。"""
        a, b = _session("proposer"), _session("reviewer")
        assert a.budget is not b.budget
        assert isinstance(a.budget, PeerBudget)


class TestWaitWhilePaused:
    @pytest.mark.asyncio
    async def test_not_paused_returns_state(self):
        reg = iso.LiveSubagentRegistry()
        session = reg.register(_session())
        assert await iso.wait_while_paused(reg, session.session_id) == iso.SESSION_ALIVE

    @pytest.mark.asyncio
    async def test_resume_unblocks(self):
        reg = iso.LiveSubagentRegistry(policy=iso.IsolationPolicy(max_pause_wait_seconds=3))
        session = reg.register(_session())
        reg.pause(session.session_id)

        async def _resume():
            await asyncio.sleep(0.06)
            reg.resume(session.session_id)

        task = asyncio.ensure_future(_resume())
        assert await iso.wait_while_paused(reg, session.session_id) == iso.SESSION_ALIVE
        await task

    @pytest.mark.asyncio
    async def test_kill_unblocks(self):
        reg = iso.LiveSubagentRegistry(policy=iso.IsolationPolicy(max_pause_wait_seconds=3))
        session = reg.register(_session())
        reg.pause(session.session_id)

        async def _kill():
            await asyncio.sleep(0.06)
            reg.kill(session.session_id)

        task = asyncio.ensure_future(_kill())
        assert await iso.wait_while_paused(reg, session.session_id) == iso.SESSION_KILLED
        await task

    @pytest.mark.asyncio
    async def test_timeout_fail_open(self):
        pol = iso.IsolationPolicy(max_pause_wait_seconds=0.01)
        reg = iso.LiveSubagentRegistry(policy=pol)
        session = reg.register(_session())
        reg.pause(session.session_id)
        started = time.monotonic()
        result = await iso.wait_while_paused(reg, session.session_id, policy=pol)
        assert result == iso.SESSION_ALIVE  # 超时放行，绝不悬挂
        assert time.monotonic() - started < 2

    @pytest.mark.asyncio
    async def test_unknown_session_killed(self):
        reg = iso.LiveSubagentRegistry()
        assert await iso.wait_while_paused(reg, "missing") == iso.SESSION_KILLED


# ---------------------------------------------------------------------------
# 4. 任务面 state / config 隔离
# ---------------------------------------------------------------------------


class TestEphemeralStateAndConfig:
    def test_state_trimmed_to_last_message(self):
        state = _state()
        clone = iso.ephemeral_task_face_state(state)
        assert clone is not state
        assert [m.content for m in clone.messages] == ["新问题"]
        assert clone.router is state.router  # 任务态字段共享

    def test_state_empty_messages_passthrough(self):
        state = AgentState(messages=[])
        clone = iso.ephemeral_task_face_state(state)
        assert list(clone.messages) == []

    def test_config_strips_live_memory_and_injects_snapshot(self):
        snap = iso.take_memory_snapshot("记忆正文")
        cfg = {"configurable": {"memory_context": "记忆正文", "session_id": "s1"}}
        out = iso.isolated_config(cfg, snap)
        assert out is not cfg
        assert "memory_context" not in out["configurable"]
        assert out["configurable"]["memory_snapshot"] is snap
        assert out["configurable"]["session_id"] == "s1"
        # 原 config 不被就地修改
        assert cfg["configurable"]["memory_context"] == "记忆正文"

    def test_config_without_snapshot_keeps_no_memory_key(self):
        out = iso.isolated_config({"configurable": {"session_id": "s"}}, None)
        assert "memory_context" not in out["configurable"]
        assert "memory_snapshot" not in out["configurable"]

    def test_config_non_dict_identity(self):
        assert iso.isolated_config(None, None) is None


# ---------------------------------------------------------------------------
# 5. wrap_peer（心跳 / steer / 快照段 / kill 短路）
# ---------------------------------------------------------------------------


class TestWrapPeer:
    @staticmethod
    def _inner(calls: list):
        async def inner(*, goal, context, round, **kw):
            calls.append({"goal": goal, "context": context, "round": round})
            return PeerResult(
                agent_id="proposer",
                role=AgentRole.PROPOSER,
                output="草稿",
                meta={"decision": "accept"},
            )

        return inner

    @pytest.mark.asyncio
    async def test_passthrough_and_heartbeat(self):
        reg = iso.LiveSubagentRegistry()
        session = reg.register(_session())
        session.last_heartbeat = time.time() - 999  # 过期
        calls: list = []
        wrapped = iso.wrap_peer(
            self._inner(calls),
            registry=reg,
            session=session,
            role=AgentRole.PROPOSER,
        )
        out = await wrapped(goal="g", context="黑板", round=1)
        assert out.output == "草稿"
        assert calls[0]["context"] == "黑板"
        assert time.time() - session.last_heartbeat < 5  # 心跳已刷新

    @pytest.mark.asyncio
    async def test_steering_injected_once_with_marker(self):
        reg = iso.LiveSubagentRegistry()
        session = reg.register(_session())
        reg.steer(session.session_id, "注意利息上限")
        calls: list = []
        wrapped = iso.wrap_peer(
            self._inner(calls), registry=reg, session=session, role=AgentRole.PROPOSER
        )
        out = await wrapped(goal="g", context="黑板", round=1)
        assert "[steering] 注意利息上限" in calls[0]["context"]
        assert out.meta["steered"] is True
        # 一次性：下一轮无指令、无标记
        out2 = await wrapped(goal="g", context="黑板", round=2)
        assert "[steering]" not in calls[1]["context"]
        assert "steered" not in out2.meta

    @pytest.mark.asyncio
    async def test_snapshot_section_appended(self):
        reg = iso.LiveSubagentRegistry()
        session = reg.register(_session())
        snap = iso.take_memory_snapshot("记忆正文")
        calls: list = []
        wrapped = iso.wrap_peer(
            self._inner(calls),
            registry=reg,
            session=session,
            role=AgentRole.PROPOSER,
            snapshot=snap,
        )
        await wrapped(goal="g", context="黑板", round=1)
        assert "【父记忆快照（只读）】" in calls[0]["context"]
        assert "记忆正文" in calls[0]["context"]

    @pytest.mark.asyncio
    async def test_kill_short_circuits_inner(self):
        reg = iso.LiveSubagentRegistry()
        session = reg.register(_session())
        reg.kill(session.session_id)
        calls: list = []
        wrapped = iso.wrap_peer(
            self._inner(calls), registry=reg, session=session, role=AgentRole.PROPOSER
        )
        out = await wrapped(goal="g", context="黑板", round=1)
        assert isinstance(out, PeerResult)
        assert out.meta["session"] == iso.SESSION_KILLED
        assert out.meta["decision"] == "reject"
        assert calls == []  # 内层零执行

    @pytest.mark.asyncio
    async def test_paused_then_resumed_runs_inner(self):
        reg = iso.LiveSubagentRegistry(
            policy=iso.IsolationPolicy(max_pause_wait_seconds=3)
        )
        session = reg.register(_session())
        reg.pause(session.session_id)
        calls: list = []
        wrapped = iso.wrap_peer(
            self._inner(calls), registry=reg, session=session, role=AgentRole.PROPOSER
        )

        async def _resume():
            await asyncio.sleep(0.06)
            reg.resume(session.session_id)

        task = asyncio.ensure_future(_resume())
        out = await wrapped(goal="g", context="黑板", round=1)
        await task
        assert out.output == "草稿"
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_killed_while_paused_short_circuits(self):
        reg = iso.LiveSubagentRegistry(
            policy=iso.IsolationPolicy(max_pause_wait_seconds=3)
        )
        session = reg.register(_session())
        reg.pause(session.session_id)
        calls: list = []
        wrapped = iso.wrap_peer(
            self._inner(calls), registry=reg, session=session, role=AgentRole.PROPOSER
        )

        async def _kill():
            await asyncio.sleep(0.06)
            reg.kill(session.session_id)

        task = asyncio.ensure_future(_kill())
        out = await wrapped(goal="g", context="黑板", round=1)
        await task
        assert out.meta["session"] == iso.SESSION_KILLED
        assert calls == []


# ---------------------------------------------------------------------------
# 6. supervisor 接线（flag-off 逐位等价 / flag-on 三件套 + 观测）
# ---------------------------------------------------------------------------


class _ProbingManager:
    """替身 Manager：捕获 kwargs，可按需调用 proposer（flag-on 闸验证）。"""

    probe: dict = {}

    def __init__(self, **kwargs):
        _ProbingManager.probe["kwargs"] = kwargs

    async def run(self, goal, **kw):
        if _ProbingManager.probe.get("invoke_proposer"):
            _ProbingManager.probe["p_out"] = await _ProbingManager.probe["kwargs"][
                "proposer"
            ](goal=goal, context="黑板", round=1)
        return ManagerOutcome(
            ok=True,
            final_output="草稿",
            failure_mode=FailureMode.NONE,
            rounds=1,
            reason="converged",
        )


@pytest.fixture
def _iso_spy(monkeypatch):
    """捕获 _new_isolation 产物（registry/policy/sessions）；可注入前置动作。"""
    captured: dict = {}
    original = lg_supervisor._new_isolation

    def spy(before=None):
        registry, policy, sessions = original()
        if before is not None:
            before(registry, policy, sessions)
        captured["iso"] = (registry, policy, sessions)
        return registry, policy, sessions

    monkeypatch.setattr(lg_supervisor, "_new_isolation", lambda: spy())
    monkeypatch.setattr(lg_supervisor, "Manager", _ProbingManager)
    _ProbingManager.probe = {"kwargs": {}, "invoke_proposer": False}
    return captured, spy


class TestSupervisorFlagOff:
    @pytest.mark.asyncio
    async def test_baseline_byte_equivalence(self, monkeypatch):
        handler = _RecordingHandler()
        monkeypatch.setitem(lg_supervisor.ROUTE_HANDLERS, "respond_to_general_query", handler)
        state = _state()
        config = {"configurable": {"memory_context": "记忆正文", "session_id": "s"}}

        result = await lg_supervisor.supervisor_dispatch(state, config=config)

        assert handler.state is state  # 原对象直通
        assert handler.config is config
        assert handler.config["configurable"]["memory_context"] == "记忆正文"
        assert [m.content for m in handler.state.messages] == ["旧问题", "新问题"]
        meta = result["messages"][-1].additional_kwargs["multi_agent"]
        assert "isolation" not in meta
        assert meta["exit_reason"] == "completed"

    @pytest.mark.asyncio
    async def test_manager_receives_no_budgets(self, monkeypatch, _iso_spy):
        captured, spy = _iso_spy
        monkeypatch.setattr(settings, FLAG, False)
        monkeypatch.setitem(lg_supervisor.ROUTE_HANDLERS, "respond_to_general_query", _RecordingHandler())

        await lg_supervisor.supervisor_dispatch(_state(), config={})
        assert "budgets" not in _ProbingManager.probe["kwargs"]
        assert "iso" not in captured  # _new_isolation 未被调用


class TestSupervisorFlagOn:
    @pytest.mark.asyncio
    async def test_task_face_trim_and_memory_strip_direct(self, monkeypatch):
        monkeypatch.setattr(settings, FLAG, True)
        handler = _RecordingHandler()
        monkeypatch.setitem(lg_supervisor.ROUTE_HANDLERS, "respond_to_general_query", handler)
        state = _state()
        config = {"configurable": {"memory_context": "记忆正文", "session_id": "s"}}

        await lg_supervisor.supervisor_dispatch(state, config=config)

        assert handler.state is not state
        assert [m.content for m in handler.state.messages] == ["新问题"]
        assert handler.state.router is state.router
        dyn = handler.config["configurable"]
        assert "memory_context" not in dyn
        assert isinstance(dyn["memory_snapshot"], iso.MemorySnapshot)
        assert dyn["memory_snapshot"].text == "记忆正文"
        assert dyn["session_id"] == "s"
        # 原 config 未被就地修改（活记忆仍在调用方手里）
        assert config["configurable"]["memory_context"] == "记忆正文"

    @pytest.mark.asyncio
    async def test_isolation_meta_block(self, monkeypatch):
        monkeypatch.setattr(settings, FLAG, True)
        monkeypatch.setitem(lg_supervisor.ROUTE_HANDLERS, "respond_to_general_query", _RecordingHandler())

        result = await lg_supervisor.supervisor_dispatch(
            _state(), config={"configurable": {"memory_context": "记忆正文"}}
        )
        meta = result["messages"][-1].additional_kwargs["multi_agent"]
        block = meta["isolation"]
        assert set(block) == {"policy", "sessions", "snapshot_chars", "registry"}
        assert block["policy"]["ephemeral_prompt"] is True
        assert block["policy"]["skip_memory"] is True
        assert [s["actor"] for s in block["sessions"]] == ["proposer", "reviewer"]
        assert all(len(s["session_id"]) == 32 for s in block["sessions"])
        assert block["snapshot_chars"] == len("记忆正文")
        assert block["registry"]["counts"] == {"alive": 2, "paused": 0, "killed": 0}

    @pytest.mark.asyncio
    async def test_no_memory_context_means_zero_snapshot(self, monkeypatch):
        monkeypatch.setattr(settings, FLAG, True)
        monkeypatch.setitem(lg_supervisor.ROUTE_HANDLERS, "respond_to_general_query", _RecordingHandler())

        result = await lg_supervisor.supervisor_dispatch(_state(), config={})
        meta = result["messages"][-1].additional_kwargs["multi_agent"]
        assert meta["isolation"]["snapshot_chars"] == 0

    @pytest.mark.asyncio
    async def test_manager_budgets_are_session_budgets(self, monkeypatch, _iso_spy):
        captured, spy = _iso_spy
        monkeypatch.setattr(settings, FLAG, True)
        monkeypatch.setitem(lg_supervisor.ROUTE_HANDLERS, "respond_to_general_query", _RecordingHandler())

        await lg_supervisor.supervisor_dispatch(_state(), config={})
        registry, _policy, sessions = captured["iso"]
        budgets = _ProbingManager.probe["kwargs"]["budgets"]
        assert budgets == {"proposer": sessions[0].budget, "reviewer": sessions[1].budget}
        assert registry.get(sessions[0].session_id) is sessions[0]

    @pytest.mark.asyncio
    async def test_kill_gate_blocks_proposer(self, monkeypatch, _iso_spy):
        captured, spy = _iso_spy
        monkeypatch.setattr(settings, FLAG, True)
        monkeypatch.setitem(lg_supervisor.ROUTE_HANDLERS, "respond_to_general_query", _RecordingHandler())

        def _kill_proposer(registry, _policy, sessions):
            registry.kill(sessions[0].session_id)

        monkeypatch.setattr(lg_supervisor, "_new_isolation", lambda: spy(before=_kill_proposer))

        _ProbingManager.probe["invoke_proposer"] = True
        await lg_supervisor.supervisor_dispatch(_state(), config={})
        out = _ProbingManager.probe["p_out"]
        assert isinstance(out, PeerResult)
        assert out.meta["session"] == "killed"
        assert out.output == ""

    @pytest.mark.asyncio
    async def test_steer_and_heartbeat_through_dispatch(self, monkeypatch, _iso_spy):
        captured, spy = _iso_spy
        monkeypatch.setattr(settings, FLAG, True)
        monkeypatch.setitem(lg_supervisor.ROUTE_HANDLERS, "respond_to_general_query", _RecordingHandler())

        def _prime(registry, _policy, sessions):
            sessions[0].last_heartbeat = time.time() - 999
            registry.steer(sessions[0].session_id, "注意利息上限")

        monkeypatch.setattr(lg_supervisor, "_new_isolation", lambda: spy(before=_prime))

        _ProbingManager.probe["invoke_proposer"] = True
        await lg_supervisor.supervisor_dispatch(_state(), config={})
        registry, _policy, sessions = captured["iso"]
        out = _ProbingManager.probe["p_out"]
        assert out.meta.get("steered") is True
        assert time.time() - sessions[0].last_heartbeat < 5  # 心跳已刷新


# ---------------------------------------------------------------------------
# 7. 配置键五处同步（键面守卫；p41 表另测）
# ---------------------------------------------------------------------------


class TestKeySync:
    def test_settings_default_false(self):
        assert settings.MULTIAGENT_SUBAGENT_ISOLATION_ENABLED is False

    def test_selfcheck_knows_key(self):
        from agentix.config import selfcheck

        assert "MULTIAGENT_SUBAGENT_ISOLATION_ENABLED" in selfcheck.DEFAULT_IGNORED_MISSING

    def test_env_example_active_line(self):
        # 触发模块加载（显式弃值：原写法被 F841 判未用，语义不变）
        _ = lg_supervisor.__doc__ or ""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        env_text = (root / ".env.example").read_text(encoding="utf-8")
        active = [
            line
            for line in env_text.splitlines()
            if line.strip().startswith("MULTIAGENT_SUBAGENT_ISOLATION_ENABLED=")
        ]
        assert active == ["MULTIAGENT_SUBAGENT_ISOLATION_ENABLED=false"]

    def test_configuration_md_row(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        text = (root / "docs" / "architecture" / "configuration.md").read_text(
            encoding="utf-8"
        )
        assert "`MULTIAGENT_SUBAGENT_ISOLATION_ENABLED`" in text
        assert "`false`" in text
