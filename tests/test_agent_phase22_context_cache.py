"""Phase 22 — 上下文二期：缓存纪律（工具序冻结 / 配置变更 append）+ L4 auto-compact。

覆盖 DoD 五项：
1. 同配置两次 schema 哈希一致；``CONTEXT_AUTOCOMPACT`` 开关不进前缀内容；
2. 运行中改配置 → 轨迹 append ``system_notice``，静态前缀字节不变；
3. L4 off：与 Phase 18 行为逐字节等价（回归）；
4. L4 on + FakeCompactor：五要素机制断言通过；超预算仍 deterministic 终止；
5. 冻结快照后动态注册被拒（Gotcha 1）。

运行：./.venv/Scripts/python.exe -m pytest tests/test_agent_phase22_context_cache.py -q
"""

from __future__ import annotations

import pytest
from loguru import logger as loguru_logger

from agentix.agent.context import ContextEngine, SimpleContextBuilder, build_instructions
from agentix.agent.context.compaction import missing_elements
from agentix.application.services.config_watch import (
    notice_if_changed,
    reset_config_watch,
)
from agentix.tools.definitions import ToolRegistry, ToolSpec, schema_prefix_hash
from agentix.tools.registry_init import ensure_registered
from agentix.config.settings import settings
from agentix.domain.agent.types import Observation, TaskSpec

# L4 flag 基线（默认关——任何测试都必须还原）
assert settings.CONTEXT_AUTOCOMPACT is False, "CONTEXT_AUTOCOMPACT 基线必须为 False"


def _task(goal: str = "G") -> TaskSpec:
    return TaskSpec(goal=goal, task_id="t22")


def _over_budget_trajectory() -> list[Observation]:
    """含五要素 token 且显著超预算的轨迹（budget 80 ≈ 320 chars）。"""
    return [
        Observation(
            step_id="s1",
            content=(
                "trace_id=abc123 constraint=keep-me obs://doc-9 status=running "
                + "word " * 40
            ),
        ),
        Observation(
            step_id="s2",
            content="error_code=E42 " + "word " * 40,
            ok=False,
        ),
    ]


class _FakeCompactor:
    """离线压缩器（Gotcha 2：L4 不得依赖真 LLM）。"""

    def __init__(self, result: str | None = None, exc: Exception | None = None) -> None:
        self.calls = 0
        self._result = result
        self._exc = exc

    async def compact(self, text: str, *, budget_tokens: int) -> str:
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result


_KEPT_ALL = (
    "trace_id=abc123 constraint=keep-me obs://doc-9 status=running "
    "[s2|fail] error_code=E42 condensed body"
)


@pytest.fixture(autouse=True)
def _clean_config_watch():
    """隔离 config_watch 模块级基线（防跨测试污染）。"""
    reset_config_watch()
    yield
    reset_config_watch()


# ---------------------------------------------------------------------------
# DoD 1：工具 schema 前缀哈希稳定 + CONTEXT_AUTOCOMPACT 不进前缀
# ---------------------------------------------------------------------------


class TestSchemaPrefixHash:
    def test_two_calls_identical(self):
        ensure_registered()
        h1 = schema_prefix_hash()
        h2 = schema_prefix_hash()
        assert h1 == h2
        assert len(h1) == 64 and all(c in "0123456789abcdef" for c in h1)

    def test_explicit_sequence_stable(self):
        ensure_registered()
        from agentix.tools.registry_init import get_active_tools

        classes = get_active_tools()
        assert schema_prefix_hash(classes) == schema_prefix_hash(classes)

    def test_context_autocompact_flag_not_in_prefix(self, monkeypatch):
        ensure_registered()
        h_off = schema_prefix_hash()
        instr_off = build_instructions()
        monkeypatch.setattr(settings, "CONTEXT_AUTOCOMPACT", True)
        assert schema_prefix_hash() == h_off
        instr_on = build_instructions()
        assert instr_on == instr_off
        assert "CONTEXT_AUTOCOMPACT" not in instr_on


# ---------------------------------------------------------------------------
# Gotcha 1：冻结快照拒绝运行期动态注册（本地 registry，不动全局）
# ---------------------------------------------------------------------------


class TestToolSchemaFreeze:
    @staticmethod
    def _spec(name: str) -> ToolSpec:
        return ToolSpec(name=name, description=f"desc {name}")

    def test_freeze_rejects_late_registration_with_warning(self):
        reg = ToolRegistry()
        reg.register(self._spec("a"), lambda: None)
        snap = reg.freeze()
        assert snap == ("a",)
        assert reg.is_frozen()

        captured: list[str] = []
        sink_id = loguru_logger.add(captured.append, level="WARNING")
        try:
            reg.register(self._spec("b"), lambda: None)
        finally:
            loguru_logger.remove(sink_id)
        assert not reg.has_tool("b"), "冻结后运行期注册必须被拒绝"
        assert any("frozen" in msg for msg in captured), "拒绝须伴随告警"

        # 幂等：重复 freeze 返回同一快照
        assert reg.freeze() == ("a",)

        # delete 不受限（下线工具不重排序）
        assert reg.delete_tool("a") is True

        # unfreeze 后可再注册（测试隔离）
        reg.unfreeze()
        reg.register(self._spec("b"), lambda: None)
        assert reg.has_tool("b")

    def test_freeze_does_not_touch_global_registry(self):
        from agentix.tools import registry as global_registry

        reg = ToolRegistry()
        reg.freeze()
        assert reg.is_frozen()
        assert not global_registry.is_frozen()


# ---------------------------------------------------------------------------
# DoD 2：配置变更 append system_notice，静态前缀字节不变
# ---------------------------------------------------------------------------


class _FakeRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record(self, **kwargs) -> None:
        self.calls.append(kwargs)


class TestConfigWatchSystemNotice:
    def test_first_call_records_baseline_then_detects_change(self):
        rec = _FakeRecorder()
        assert notice_if_changed(recorder=rec) is False  # 首调只记基线
        assert notice_if_changed(recorder=rec) is False  # 无变更

        old = settings.MAX_HISTORY_MESSAGES
        try:
            settings.MAX_HISTORY_MESSAGES = old + 1
            assert notice_if_changed(recorder=rec) is True
            assert notice_if_changed(recorder=rec) is False  # 已通知过
        finally:
            settings.MAX_HISTORY_MESSAGES = old

        assert len(rec.calls) == 1
        call = rec.calls[0]
        assert call["kind"] == "notice"
        assert call["name"] == "system_notice"
        detail = call["detail"]
        assert detail["event"] == "config_change"
        assert "MAX_HISTORY_MESSAGES" in detail["changed_keys"]
        # Gotcha 3：detail 只含键名/事件/哈希——不含任何配置值（无 PII）
        assert set(detail) == {"event", "changed_keys", "config_hash"}

    @pytest.mark.asyncio
    async def test_static_prefix_bytes_unchanged_by_notice(self):
        eng = ContextEngine(status_bar={"route": "chat"}, system_rules=("no invent",))
        before_instr = build_instructions()
        before_snap = await eng.build(_task("goal x"))

        rec = _FakeRecorder()
        notice_if_changed(recorder=rec)
        old = settings.MAX_HISTORY_MESSAGES
        try:
            settings.MAX_HISTORY_MESSAGES = old + 1
            assert notice_if_changed(recorder=rec) is True
        finally:
            settings.MAX_HISTORY_MESSAGES = old

        after_snap = await eng.build(_task("goal x"))
        assert build_instructions() == before_instr
        assert after_snap.prompt == before_snap.prompt, "system_notice 不得改静态前缀"

    def test_notice_never_contains_config_values(self):
        rec = _FakeRecorder()
        notice_if_changed(recorder=rec)
        old = settings.MAX_HISTORY_MESSAGES
        try:
            settings.MAX_HISTORY_MESSAGES = old + 1
            notice_if_changed(recorder=rec)
        finally:
            settings.MAX_HISTORY_MESSAGES = old
        assert rec.calls
        detail = rec.calls[0]["detail"]
        # 随机 config_hash（hex）可能偶发含数字子串（如 '21'）——先剔除哈希再
        # 断言，防事件 id 撞字串造成假阳性；配置值若泄进 event/changed_keys
        # 仍会被精确命中。
        detail_str = str({k: v for k, v in detail.items() if k != "config_hash"})
        assert str(old + 1) not in detail_str


# ---------------------------------------------------------------------------
# DoD 3：L4 off ≡ Phase 18（逐字节）
# ---------------------------------------------------------------------------


class TestL4OffByteEquivalent:
    @pytest.mark.asyncio
    async def test_engine_with_compactor_flag_off_identical(self):
        fake = _FakeCompactor(result=_KEPT_ALL)
        traj = _over_budget_trajectory()
        with_c = ContextEngine(status_bar={"route": "chat"}, system_rules=("RULE",), compactor=fake)
        without = ContextEngine(status_bar={"route": "chat"}, system_rules=("RULE",))
        s1 = await with_c.build(_task("goal"), trajectory=traj, budget_tokens=80)
        s2 = await without.build(_task("goal"), trajectory=traj, budget_tokens=80)
        assert s1.prompt == s2.prompt
        assert s1.truncated == s2.truncated
        assert s1.token_estimate == s2.token_estimate
        assert fake.calls == 0, "flag 关时压缩器不得被调用"

    @pytest.mark.asyncio
    async def test_simple_builder_flag_off_identical(self):
        fake = _FakeCompactor(result=_KEPT_ALL)
        traj = _over_budget_trajectory()
        with_c = SimpleContextBuilder(rules=("RULE",), compactor=fake)
        without = SimpleContextBuilder(rules=("RULE",))
        s1 = await with_c.build(_task("goal"), trajectory=traj, budget_tokens=80)
        s2 = await without.build(_task("goal"), trajectory=traj, budget_tokens=80)
        assert s1.prompt == s2.prompt
        assert s1.truncated == s2.truncated
        assert fake.calls == 0

    @pytest.mark.asyncio
    async def test_no_compactor_param_constructor_backcompat(self):
        # 既有构造签名零破坏（compactor 为可选注入）
        eng = ContextEngine()
        snap = await eng.build(_task())
        assert snap.prompt.endswith("G")


# ---------------------------------------------------------------------------
# DoD 4：L4 on + FakeCompactor——五要素断言 + 确定性终止
# ---------------------------------------------------------------------------


class TestL4On:
    @pytest.mark.asyncio
    async def test_engine_compacts_and_preserves_five_elements(self, monkeypatch):
        monkeypatch.setattr(settings, "CONTEXT_AUTOCOMPACT", True)
        fake = _FakeCompactor(result=_KEPT_ALL)
        eng = ContextEngine(status_bar={"route": "chat"}, compactor=fake)
        snap = await eng.build(_task("goal"), trajectory=_over_budget_trajectory(), budget_tokens=80)
        assert fake.calls == 1, "超预算且 flag 开 ⇒ 恰好一次压缩尝试"
        assert snap.truncated is True
        for token in (
            "trace_id=abc123",
            "constraint=keep-me",
            "obs://doc-9",
            "status=running",
            "[s2|fail]",
            "error_code=E42",
        ):
            assert token in snap.prompt, f"五要素丢失：{token}"

    @pytest.mark.asyncio
    async def test_bad_compactor_rejected_falls_back_to_drop_loop(self, monkeypatch):
        monkeypatch.setattr(settings, "CONTEXT_AUTOCOMPACT", True)
        # 压缩结果丢掉全部五要素 ⇒ 机制断言拒绝 ⇒ 回退 Phase 18 丢弃
        fake = _FakeCompactor(result="bland text without markers " * 20)
        eng = ContextEngine(compactor=fake)
        control = ContextEngine()
        traj = _over_budget_trajectory()
        s1 = await eng.build(_task("goal"), trajectory=traj, budget_tokens=80)
        s2 = await control.build(_task("goal"), trajectory=traj, budget_tokens=80)
        assert fake.calls == 1, "尝试过一次（断言拒绝而非跳过）"
        assert s1.prompt == s2.prompt, "拒绝后必须与无压缩器控制组逐字节一致"
        assert s1.truncated == s2.truncated

    @pytest.mark.asyncio
    async def test_raising_compactor_deterministic(self, monkeypatch):
        monkeypatch.setattr(settings, "CONTEXT_AUTOCOMPACT", True)
        fake = _FakeCompactor(exc=RuntimeError("llm down"))
        eng = ContextEngine(compactor=fake)
        control = ContextEngine()
        traj = _over_budget_trajectory()
        s1 = await eng.build(_task("goal"), trajectory=traj, budget_tokens=80)
        s2 = await control.build(_task("goal"), trajectory=traj, budget_tokens=80)
        assert fake.calls == 1
        assert s1.prompt == s2.prompt, "压缩器异常 ⇒ 丢弃回退，不抛出、确定性终止"

    @pytest.mark.asyncio
    async def test_compacted_still_over_budget_terminates(self, monkeypatch):
        monkeypatch.setattr(settings, "CONTEXT_AUTOCOMPACT", True)
        # 压缩后仍超预算（保留五要素的超长结果）⇒ 丢弃循环仍确定性清空
        huge = _KEPT_ALL + " tail" * 500
        fake = _FakeCompactor(result=huge)
        eng = ContextEngine(compactor=fake)
        snap = await eng.build(_task("goal"), trajectory=_over_budget_trajectory(), budget_tokens=80)
        assert fake.calls == 1
        assert snap.truncated is True
        assert snap.prompt == "GOAL goal", "压缩后仍超预算 ⇒ 丢弃循环清空轨迹后确定性终止"

    @pytest.mark.asyncio
    async def test_simple_builder_l4_on_parity_with_engine(self, monkeypatch):
        monkeypatch.setattr(settings, "CONTEXT_AUTOCOMPACT", True)
        fake = _FakeCompactor(result=_KEPT_ALL)
        builder = SimpleContextBuilder(rules=("RULE",), compactor=fake)
        snap = await builder.build(_task("goal"), trajectory=_over_budget_trajectory(), budget_tokens=80)
        assert fake.calls == 1
        assert snap.truncated is True
        assert "trace_id=abc123" in snap.prompt


# ---------------------------------------------------------------------------
# 五要素机制断言（纯函数单测）
# ---------------------------------------------------------------------------


class TestFiveElementAssertion:
    def test_pass_when_all_preserved(self):
        original = _KEPT_ALL + " extra prose " * 10
        assert missing_elements(original, _KEPT_ALL) == {}

    def test_detects_each_dropped_category(self):
        original = _KEPT_ALL
        cases = {
            "trace_id": _KEPT_ALL.replace("trace_id=abc123 ", ""),
            "error_record": _KEPT_ALL.replace("[s2|fail] ", "").replace("error_code=E42 ", ""),
            "constraint_key": _KEPT_ALL.replace("constraint=keep-me ", ""),
            "source_citation": _KEPT_ALL.replace("obs://doc-9 ", ""),
            "task_status": _KEPT_ALL.replace("status=running ", ""),
        }
        for dropped, compacted in cases.items():
            missing = missing_elements(original, compacted)
            assert dropped in missing, f"{dropped} 丢失必须被检出"

    def test_absent_category_is_vacuously_kept(self):
        # 原文没有的类别不构成断言（正文可缩写，不凭空要求）
        assert missing_elements("plain prose", "shorter prose") == {}
