"""P2（岗位对照·Skills/Registry/动态调度）：快照热重载 + 对账 + 执行契约。

把 P2 的 DoD 逐条钉死：

1. **Skills manifest 快照缓存 + 热重载**（2.3）：指纹只 stat 不读；
   目录增/删/改**无需 ``reset_registry``** 即被下一次访问看见；指纹未变
   **零重扫**（扫描调用计数不变）；
2. **规范即测试**（2.3）：``skill_standards_errors`` 单一真源，仓库内
   shipped skills 全部过规范（长度上限 / 正文不截断 / name==目录）；
3. **三注册表对账**（2.1）：声明清单 ⇄ spec 注册表 ⇄ handler 表漂移
   分类报告（declared_no_handler / handler_no_spec / dynamic /
   stale_exemptions），默认配置 ok、审计只读、已知旁路有署名理由；
4. **固定裁决顺序**（2.5）：risk → permission → review(ask) → observe →
   execute 的**全链序列**被断言（既有测试只逐闸单测，无序列守卫）；
5. **执行契约**（2.1）：非 mapping 入参转 failed（不外抛）；``CancelledError``
   必须传播（唯一允许的外抛）；返回值契约 flag-off 原样透传 / flag-on
   拒收非 JSON 且**不重试**。

全局单例（声明缓存 / executor / registry）在夹具里成对复位，
与 Phase 21/23/P1 测试同款范式。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import List

import pytest

import agentix.agent.skills.registry as skills_registry_mod
from agentix.agent.skills.loader import (
    MAX_ANTI_WHEN_CHARS,
    MAX_DESCRIPTION_CHARS,
    MAX_SKILL_BODY_CHARS,
    SkillMeta,
    scan_fingerprint,
    scan_skills,
    skill_standards_errors,
)
from agentix.agent.skills.registry import catalog_block, get_registry, reset_registry
from agentix.tools import registry_init
from agentix.tools.definitions import ToolSpec, registry as tool_registry
from agentix.tools.gateway.executor import (
    ToolExecutor,
    get_tool_executor,
    reset_tool_executor,
)
from agentix.tools.adapters.integrity import (
    KNOWN_BYPASS_TOOLS,
    audit_tool_registries,
)
from agentix.config.settings import settings

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SHIPPED_SKILLS = _REPO_ROOT / "skills"

_DUMMY = "p50_dummy"
_RISKY = "p50_risky"
_SEQ = "p50_seq"
_GHOST = "p50_ghost"


def _clean_decl_state() -> None:
    """清声明缓存 + 摘掉测试挂进全局 registry 的 spec。"""
    registry_init._DECLARATIONS.clear()
    for name in (_DUMMY, _RISKY, _SEQ):
        tool_registry.delete_tool(name)


@pytest.fixture
def baseline(monkeypatch: pytest.MonkeyPatch):
    """默认 flag 态（全部关）+ 三单例复位。"""
    for key in (
        "SKILLS_ENABLED",
        "NOTIFY_USER_ENABLED",
        "TASK_EXEC_ENABLED",
        "TOOL_SELECTION_ENABLED",
        "TOOL_RESULT_CONTRACT_ENABLED",
    ):
        monkeypatch.setattr(settings, key, False)
    monkeypatch.setattr(settings, "TOOL_DEFERRED_TOOLS", "")
    _clean_decl_state()
    reset_tool_executor()
    reset_registry()
    yield
    _clean_decl_state()
    reset_tool_executor()
    reset_registry()


# ---------------------------------------------------------------------------
# 1. Skills manifest 快照缓存 + 热重载（2.3）
# ---------------------------------------------------------------------------


def _write_skill(root: Path, dirname: str, name: str, description: str) -> Path:
    skill_dir = root / dirname
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "anti_when: 不适用时\n"
        "---\n# 正文\n\n步骤。\n",
        encoding="utf-8",
    )
    return path


class TestSkillSnapshotRefresh:
    def test_fingerprint_shape_and_stability(self, tmp_path: Path):
        _write_skill(tmp_path, "alpha", "alpha", "第一条技能")
        fp1 = scan_fingerprint(tmp_path)
        fp2 = scan_fingerprint(tmp_path)
        assert fp1 == fp2
        assert len(fp1) == 1
        rel, mtime_ns, size = fp1[0]
        assert rel == "alpha/SKILL.md"
        assert isinstance(mtime_ns, int) and size > 0

    def test_fingerprint_missing_root_is_empty(self, tmp_path: Path):
        assert scan_fingerprint(tmp_path / "nope") == ()

    def test_new_skill_visible_without_reset(self, tmp_path: Path, monkeypatch):
        _write_skill(tmp_path, "alpha", "alpha", "第一条技能")
        monkeypatch.setattr(settings, "SKILLS_DIR", str(tmp_path))
        reset_registry()
        assert "alpha" in catalog_block()

        _write_skill(tmp_path, "beta", "beta", "第二条技能")
        # 故意不 reset_registry——热重载必须让下一次访问看见新条目
        block = catalog_block()
        assert "alpha" in block and "beta" in block
        assert get_registry().maybe_refresh() is False  # 已收敛，二次无重扫

    def test_modify_and_remove_detected(self, tmp_path: Path, monkeypatch):
        path = _write_skill(tmp_path, "alpha", "alpha", "旧描述旧描述旧描述")
        monkeypatch.setattr(settings, "SKILLS_DIR", str(tmp_path))
        reset_registry()
        assert "旧描述" in catalog_block()

        path.write_text(
            "---\nname: alpha\ndescription: 新描述新描述新描述\n"
            "anti_when: 不适用时\n---\n# 正文\n",
            encoding="utf-8",
        )
        assert "新描述" in catalog_block()
        assert "旧描述" not in catalog_block()

        path.unlink()
        (tmp_path / "alpha").rmdir()
        assert get_registry().names() == ()

    def test_unchanged_fingerprint_never_rescans(
        self, tmp_path: Path, monkeypatch
    ):
        _write_skill(tmp_path, "alpha", "alpha", "第一条技能")
        monkeypatch.setattr(settings, "SKILLS_DIR", str(tmp_path))
        reset_registry()

        calls = {"n": 0}
        real_scan = skills_registry_mod.scan_skills

        def _counting_scan(root):
            calls["n"] += 1
            return real_scan(root)

        monkeypatch.setattr(skills_registry_mod, "scan_skills", _counting_scan)
        get_registry()  # 先完成首建（含初始一次扫描）
        before = calls["n"]
        for _ in range(5):
            get_registry()
        assert calls["n"] == before  # 指纹未变 ⇒ 零重扫（只付 stat 成本）

        _write_skill(tmp_path, "beta", "beta", "第二条技能")
        get_registry()
        assert calls["n"] == before + 1  # 变了恰好重扫一次
        assert "beta" in catalog_block()


# ---------------------------------------------------------------------------
# 2. 规范即测试（2.3）
# ---------------------------------------------------------------------------


class TestAuthoringStandards:
    def test_shipped_skills_meet_standards(self):
        """仓库内全部 shipped SKILL.md 过作者规范（规范即测试）。"""
        metas = scan_skills(_SHIPPED_SKILLS)
        assert metas, f"未扫描到任何 shipped skill：{_SHIPPED_SKILLS}"
        for meta in metas:
            body_chars = meta.path.read_text(encoding="utf-8", errors="replace").__len__()
            errors = skill_standards_errors(meta, body_chars=body_chars)
            assert not errors, f"{meta.path}: {errors}"

    def test_standards_detect_length_violations(self, tmp_path: Path):
        path = _write_skill(tmp_path, "verbose", "verbose", "x")
        meta = SkillMeta(
            name="verbose",
            description="长" * (MAX_DESCRIPTION_CHARS + 1),
            anti_when="短",
            path=path,
            root=tmp_path,
        )
        errors = skill_standards_errors(meta, body_chars=0)
        assert any("description" in e for e in errors)

        meta2 = SkillMeta(
            name="verbose",
            description="短",
            anti_when="否" * (MAX_ANTI_WHEN_CHARS + 1),
            path=path,
            root=tmp_path,
        )
        assert any("anti_when" in e for e in skill_standards_errors(meta2))

    def test_standards_detect_body_overflow_and_dir_mismatch(self, tmp_path: Path):
        path = _write_skill(tmp_path, "dir-a", "dir-a", "描述")
        meta = SkillMeta(
            name="dir-a",
            description="描述",
            anti_when="否",
            path=path,
            root=tmp_path,
        )
        assert not skill_standards_errors(meta, body_chars=MAX_SKILL_BODY_CHARS)
        assert any(
            "body" in e for e in skill_standards_errors(meta, body_chars=MAX_SKILL_BODY_CHARS + 1)
        )

        renamed = SkillMeta(
            name="other-name",
            description="描述",
            anti_when="否",
            path=path,
            root=tmp_path,
        )
        assert any(
            "directory" in e for e in skill_standards_errors(renamed, body_chars=0)
        )


# ---------------------------------------------------------------------------
# 3. 三注册表对账（2.1）
# ---------------------------------------------------------------------------


class TestRegistryAudit:
    def test_default_state_is_clean(self, baseline):
        report = audit_tool_registries()
        assert report.ok, report.summary()
        assert report.declared_no_handler == ()
        assert report.handler_no_spec == ()

    def test_known_bypass_is_documented_not_flagged(self, baseline):
        report = audit_tool_registries()
        # 4 个 KG 工具 + search 走节点/组件直连——豁免必须**有署名理由**
        for name in ("search", "cypher_query", "text2sql_query"):
            assert name in report.bypass
            assert report.bypass[name].strip()
        assert "search" not in report.declared_no_handler
        assert set(KNOWN_BYPASS_TOOLS) >= {"search", "cypher_query"}

    def test_declared_without_handler_is_drift(self, baseline, monkeypatch):
        # 声明在册（flag-on）但 handler 未装 ⇒ 调了必 unknown tool
        monkeypatch.setattr(settings, "TASK_EXEC_ENABLED", True)
        registry_init._DECLARATIONS.clear()
        report = audit_tool_registries()
        assert not report.ok
        assert "append_task_note" in report.declared_no_handler
        assert "write_task_artifact" in report.declared_no_handler

    def test_handler_without_spec_is_drift(self, baseline):
        executor = get_tool_executor()
        executor.register_handler(_GHOST, lambda args: {"ok": True})
        report = audit_tool_registries()
        assert not report.ok
        assert _GHOST in report.handler_no_spec
        assert _GHOST in report.dynamic_handlers  # 同时是信息项：不在声明清单

    def test_stale_exemption_reported_when_handler_appears(self, baseline):
        # search 一旦被装上 handler，豁免就过期了——报告要能提示撤豁免
        executor = get_tool_executor()
        executor.register_handler("search", lambda args: {"ok": True})
        report = audit_tool_registries()
        assert "search" in report.stale_exemptions

    def test_audit_is_read_only(self, baseline):
        executor = get_tool_executor()
        executor.register_handler(_GHOST, lambda args: None)
        before_handlers = executor.handler_names()
        before_decls = tuple(n for n, _ in registry_init.get_declarations())
        audit_tool_registries()
        assert executor.handler_names() == before_handlers
        assert tuple(n for n, _ in registry_init.get_declarations()) == before_decls


# ---------------------------------------------------------------------------
# 4. 固定裁决顺序：deny → permission → ask → observe → execute（2.5）
# ---------------------------------------------------------------------------


def _register(name: str, spec_kwargs: dict, handler) -> None:
    spec = ToolSpec(name=name, description=name, **spec_kwargs)
    if not tool_registry.has_tool(name):
        tool_registry.register(spec, lambda: None)
    get_tool_executor().register_handler(name, handler, spec=spec)


class _OrderGate:
    """记录 preflight 调用顺序的假审批闸门（放行或按需抛错）。"""

    def __init__(self, order: List[str], *, fail: bool = False) -> None:
        self._order = order
        self._fail = fail

    def preflight(self, tool_name, args, **kwargs):
        self._order.append("review")
        if self._fail:
            raise RuntimeError("gate says no")


class TestGateOrderSequence:
    @pytest.mark.asyncio
    async def test_full_chain_order_is_fixed(self, baseline):
        order: List[str] = []
        executor = ToolExecutor(
            permission_checker=lambda n, s, p: order.append("permission") or True,
            review_gate=_OrderGate(order),
        )
        _register(_SEQ, {"risk_level": "L0"}, lambda args: order.append("handler") or {"ok": True})
        executor.register_handler(_SEQ, lambda args: order.append("handler") or {"ok": True})
        executor.set_observation_hooks(
            before=lambda *a: order.append("observe_before"),
            after=lambda *a: order.append("observe_after"),
        )
        result = await executor.execute(_SEQ, {})
        assert result.success
        assert order == [
            "permission",
            "review",
            "observe_before",
            "handler",
            "observe_after",
        ]

    @pytest.mark.asyncio
    async def test_risk_deny_precedes_permission_review_execute(self, baseline):
        order: List[str] = []
        executor = ToolExecutor(
            policy=type("P", (), {"max_risk_level": "L0"})(),
            permission_checker=lambda n, s, p: order.append("permission") or True,
            review_gate=_OrderGate(order),
        )
        _register(_RISKY, {"risk_level": "L1"}, lambda args: order.append("handler") or {})
        executor.register_handler(_RISKY, lambda args: order.append("handler") or {})
        executor.set_observation_hooks(
            before=lambda *a: order.append("observe_before"),
            after=lambda *a: order.append("observe_after"),
        )
        result = await executor.execute(_RISKY, {})
        assert not result.success
        assert result.metadata.get("reason") == "risk_denied"
        assert order == []  # 风险闸在最前：后续一个都不许发生

    @pytest.mark.asyncio
    async def test_permission_deny_precedes_review_and_execute(self, baseline):
        order: List[str] = []
        executor = ToolExecutor(
            permission_checker=lambda n, s, p: order.append("permission") or False,
            review_gate=_OrderGate(order),
        )
        _register(_SEQ, {"risk_level": "L0"}, lambda args: None)
        executor.register_handler(_SEQ, lambda args: order.append("handler") or {})
        result = await executor.execute(_SEQ, {})
        assert not result.success
        assert result.metadata.get("reason") == "permission_denied"
        assert order == ["permission"]  # review 与 handler 都没轮到

    @pytest.mark.asyncio
    async def test_review_deny_precedes_observe_and_execute(self, baseline):
        order: List[str] = []
        executor = ToolExecutor(
            permission_checker=lambda n, s, p: order.append("permission") or True,
            review_gate=_OrderGate(order, fail=True),
        )
        _register(_SEQ, {"risk_level": "L0"}, lambda args: None)
        executor.register_handler(_SEQ, lambda args: order.append("handler") or {})
        executor.set_observation_hooks(
            before=lambda *a: order.append("observe_before"),
            after=lambda *a: order.append("observe_after"),
        )
        result = await executor.execute(_SEQ, {})
        assert not result.success
        assert result.metadata.get("reason") == "review_preflight_error"
        # 被闸拒的尝试不是一次执行：observe_before/handler 绝不触发
        assert order == ["permission", "review"]


# ---------------------------------------------------------------------------
# 5. 执行契约（2.1）：入参 / 取消 / 返回值
# ---------------------------------------------------------------------------


class TestExecutionContract:
    @pytest.mark.asyncio
    async def test_non_mapping_arguments_become_failed_result(self, baseline):
        executor = get_tool_executor()
        _register(_DUMMY, {"risk_level": "L0"}, lambda args: {"ok": True})
        executor.register_handler(_DUMMY, lambda args: {"ok": True})
        result = await executor.execute(_DUMMY, 42)  # type: ignore[arg-type]
        assert not result.success
        assert result.metadata.get("reason") == "invalid_arguments"
        assert "mapping" in (result.error or "")

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self, baseline):
        """取消必须传播——这是 execute「不外抛」契约的唯一例外。"""
        executor = get_tool_executor()

        def _cancel(args):
            raise asyncio.CancelledError()

        _register(_DUMMY, {"risk_level": "L0"}, _cancel)
        executor.register_handler(_DUMMY, _cancel)
        with pytest.raises(asyncio.CancelledError):
            await executor.execute(_DUMMY, {})

    @pytest.mark.asyncio
    async def test_contract_flag_off_passthrough(self, baseline):
        executor = get_tool_executor()
        sentinel = object()  # 不可 JSON 序列化
        _register(_DUMMY, {"risk_level": "L0"}, lambda args: sentinel)
        executor.register_handler(_DUMMY, lambda args: sentinel)
        assert settings.TOOL_RESULT_CONTRACT_ENABLED is False
        result = await executor.execute(_DUMMY, {})
        assert result.success
        assert result.data is sentinel  # 原样透传，与基线逐位等价

    @pytest.mark.asyncio
    async def test_contract_flag_on_rejects_without_retry(self, baseline, monkeypatch):
        monkeypatch.setattr(settings, "TOOL_RESULT_CONTRACT_ENABLED", True)
        executor = get_tool_executor()
        calls = {"n": 0}

        def _bad(args):
            calls["n"] += 1
            return {"bad": object()}

        # retryable spec：若契约违规被当成可重试错误，会调 1+2 次
        _register(
            _DUMMY,
            {"risk_level": "L0", "retryable": True, "max_retries": 2},
            _bad,
        )
        executor.register_handler(_DUMMY, _bad)
        result = await executor.execute(_DUMMY, {})
        assert not result.success
        assert result.metadata.get("reason") == "invalid_result"
        assert result.retryable is False  # 契约违规不重试
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_contract_flag_on_accepts_json_values(self, baseline, monkeypatch):
        monkeypatch.setattr(settings, "TOOL_RESULT_CONTRACT_ENABLED", True)
        executor = get_tool_executor()
        _register(_DUMMY, {"risk_level": "L0"}, lambda args: {"ok": True, "中文": "值"})
        executor.register_handler(_DUMMY, lambda args: {"ok": True, "中文": "值"})
        result = await executor.execute(_DUMMY, {})
        assert result.success
        assert result.data == {"ok": True, "中文": "值"}
