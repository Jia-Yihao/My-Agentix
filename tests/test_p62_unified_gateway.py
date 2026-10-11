"""A2（统一执行链 Stage A）守卫测试。

覆盖 A2a–A2c 三件事的可执行契约：

1. **三闸有效化**：``TOOL_SELECTION_ENABLED`` 打开时 ``get_tool_executor()``
   注入 policy + permission_checker（flag-off = 零注入基线）；权限兜底覆盖
   DISABLED_TOOLS/白/黑名单/能力标签；HITL 决策面标记抑制误导性启动告警、
   随复位归零；
2. **五项旁路收编**：``kg_gateway.run_governed`` flag-off 直连等价 /
   flag-on 经治理链、失败抛 ``GatewayDenied``；组合根安装装齐五 handler
   （审计 stale_exemptions 属预期灰度信号）；``KNOWN_BYPASS_TOOLS`` 棘轮
   只减不增（A2 本批 = 基线五项零变化）；
3. **记忆三工具**：flag-off 不声明不装；flag-on 声明 title 契约 + 风险分级
   + remember→recall→forget roundtrip；``MemoryManager.forget`` 对未声明
   forget 的 provider 静默跳过（duck-typed 扇出，Protocol 不扩容）。
"""

from __future__ import annotations

import pytest
from loguru import logger as loguru_logger

from agentix.config import settings
import agentix.tools.registry_init as registry_init

BYPASS_BASELINE = frozenset(
    {
        "search",
        "cypher_query",
        "predefined_cypher",
        "microsoft_graphrag_query",
        "text2sql_query",
    }
)
MEMORY_TOOL_NAMES = ("remember", "recall", "forget")


@pytest.fixture(autouse=True)
def _clean_global_state():
    """每例前后复位 executor/闸门/桥/声明缓存（防跨例泄漏）。"""
    from agentix.policies.hitl.hitl_bridge import reset_hitl_bridge
    from agentix.tools.definitions import registry as tool_registry
    from agentix.tools.gateway.executor import reset_tool_executor
    from agentix.tools.gateway.human_review import reset_review_gate

    for fn in (reset_hitl_bridge, reset_review_gate, reset_tool_executor):
        fn()
    registry_init._DECLARATIONS.clear()
    yield
    for fn in (reset_hitl_bridge, reset_review_gate, reset_tool_executor):
        fn()
    registry_init._DECLARATIONS.clear()
    # flag-on 的 ensure_registered 会把 remember(L1)/forget(L1)/recall 注入
    # 全局 spec 注册表——测试完必须撤（对齐 p50/p60 模式，否则 r1 的
    # 「注册表全 L0」断言在全量顺序下红）。
    for name in MEMORY_TOOL_NAMES:
        tool_registry.delete_tool(name)


# ---------------------------------------------------------------------------
# A2a — 三闸有效化
# ---------------------------------------------------------------------------


class TestGateInjection:
    def test_flag_off_executor_is_baseline_wiring(self):
        from agentix.tools.gateway.executor import get_tool_executor

        ex = get_tool_executor()
        assert ex.policy is None
        assert ex._permission_checker is None

    def test_flag_on_injects_policy_and_permission_checker(self, monkeypatch):
        from agentix.tools.gateway.executor import get_tool_executor

        monkeypatch.setattr(settings, "TOOL_SELECTION_ENABLED", True, raising=False)
        ex = get_tool_executor()
        assert ex.policy is not None
        assert ex.policy.max_risk_level == str(settings.TOOL_SELECTION_MAX_RISK)
        assert ex._permission_checker is not None

    def test_flag_on_invalid_max_risk_falls_back_to_baseline(self, monkeypatch):
        """非法 MAX_RISK → build_tool_policy 返回 None → 零注入（不打挂单例）。"""
        from agentix.tools.gateway.executor import get_tool_executor

        monkeypatch.setattr(settings, "TOOL_SELECTION_ENABLED", True, raising=False)
        monkeypatch.setattr(settings, "TOOL_SELECTION_MAX_RISK", "L9", raising=False)
        ex = get_tool_executor()
        assert ex.policy is None
        assert ex._permission_checker is None

    def test_permission_checker_full_truth_table(self, monkeypatch):
        from agentix.tools.definitions import ToolSpec
        from agentix.tools.selection import (
    ToolSelectionPolicy,
    tool_permission_check,
)

        spec = ToolSpec(
            name="probe", description="d", capabilities=("memory",)
        )
        memory_spec = ToolSpec(name="m", description="d", capabilities=("memory",))
        plain_spec = ToolSpec(name="p", description="d")

        # policy=None（选择层未启用语义）→ 只剩 DISABLED_TOOLS 兜底
        assert tool_permission_check("probe", spec, None) is True
        monkeypatch.setattr(settings, "DISABLED_TOOLS", "probe", raising=False)
        assert tool_permission_check("probe", spec, None) is False
        monkeypatch.setattr(settings, "DISABLED_TOOLS", "", raising=False)

        # 黑名单
        denied = ToolSelectionPolicy(denied_tools=frozenset({"probe"}))
        assert tool_permission_check("probe", spec, denied) is False
        assert tool_permission_check("other", spec, denied) is True

        # 白名单（非 None 即「不在即拒」）
        allowed = ToolSelectionPolicy(allowed_tools=frozenset({"probe"}))
        assert tool_permission_check("probe", spec, allowed) is True
        assert tool_permission_check("other", spec, allowed) is False

        # 能力标签
        caps = ToolSelectionPolicy(allowed_capabilities=frozenset({"memory"}))
        assert tool_permission_check("m", memory_spec, caps) is True
        assert tool_permission_check("p", plain_spec, caps) is False
        assert tool_permission_check("x", None, caps) is False  # 无声明即无能力

        # 非法 DISABLED 名单段不误伤
        monkeypatch.setattr(settings, "DISABLED_TOOLS", " , ", raising=False)
        assert tool_permission_check("probe", spec, None) is True

    @pytest.mark.asyncio
    async def test_disabled_tool_denied_at_execution_side(self, monkeypatch):
        """执行侧兜底：DISABLED_TOOLS 中的工具在 flag-on 下被拒（permission_denied）。"""
        from agentix.tools.definitions import ToolSpec, registry as tool_registry
        from agentix.tools.gateway.executor import get_tool_executor

        monkeypatch.setattr(settings, "TOOL_SELECTION_ENABLED", True, raising=False)
        monkeypatch.setattr(settings, "DISABLED_TOOLS", "p62_probe_tool", raising=False)

        ex = get_tool_executor()
        tool_registry.register(
            ToolSpec(name="p62_probe_tool", description="probe"), lambda: None
        )
        try:
            ex.register_handler("p62_probe_tool", lambda a: {"ok": True})
            result = await ex.execute("p62_probe_tool", {})
            assert not result.success
            assert result.metadata.get("reason") == "permission_denied"
        finally:
            tool_registry.delete_tool("p62_probe_tool")


class TestDecisionSurface:
    def test_marked_surface_suppresses_start_warning(self, monkeypatch):
        from agentix.tools.gateway.human_review import (
    get_review_gate,
    mark_decision_surface,
    reset_review_gate,
)

        monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True, raising=False)
        reset_review_gate()
        sink: list = []
        handle_id = loguru_logger.add(sink.append)
        try:
            mark_decision_surface()
            gate = get_review_gate()
        finally:
            loguru_logger.remove(handle_id)
            reset_review_gate()
        assert gate.enabled is True
        assert not any("no approver is wired" in str(line) for line in sink), sink

    def test_install_marks_and_reset_clears(self):
        from agentix.policies.hitl.hitl_bridge import (
    install_executor_hook,
    reset_hitl_bridge,
)
        from agentix.tools.gateway import human_review

        try:
            install_executor_hook()
            assert human_review._decision_surface is True
        finally:
            reset_hitl_bridge()
        # 桥卸载 → 决策面撤销（下次开闸重新告警）
        assert human_review._decision_surface is False


# ---------------------------------------------------------------------------
# A2b — 五项旁路收编
# ---------------------------------------------------------------------------


class TestGatewaySeam:
    @pytest.mark.asyncio
    async def test_flag_off_is_direct_passthrough(self, monkeypatch):
        from agentix.tools.gateway.kg_gateway import run_governed

        monkeypatch.setattr(settings, "TOOL_GATEWAY_ENFORCED", False, raising=False)
        assert (
            await run_governed("cypher_query", {"statement": "X"}, direct=lambda: "RAW")
            == "RAW"
        )

        async def _coro():
            return "CORO"

        assert await run_governed("cypher_query", {}, direct=_coro) == "CORO"

    @pytest.mark.asyncio
    async def test_flag_on_without_handler_falls_back_to_direct(self, monkeypatch):
        from agentix.tools.gateway.kg_gateway import run_governed

        monkeypatch.setattr(settings, "TOOL_GATEWAY_ENFORCED", True, raising=False)
        # executor 已被夹具复位 → 无 handler → 告警回落直连（不打挂节点）
        assert (
            await run_governed("cypher_query", {"statement": "X"}, direct=lambda: "RAW")
            == "RAW"
        )

    @pytest.mark.asyncio
    async def test_flag_on_routes_via_executor_and_raises_gateway_denied(self, monkeypatch):
        from agentix.tools.definitions import ToolSpec, registry as tool_registry
        from agentix.tools.gateway.executor import get_tool_executor
        from agentix.tools.gateway.kg_gateway import GatewayDenied, run_governed

        monkeypatch.setattr(settings, "TOOL_GATEWAY_ENFORCED", True, raising=False)
        ex = get_tool_executor()
        tool_registry.register(
            ToolSpec(name="p62_seam_probe", description="probe"), lambda: None
        )
        try:
            ex.register_handler("p62_seam_probe", lambda a: {"via": "executor"})
            out = await run_governed(
                "p62_seam_probe", {"k": 1}, direct=lambda: "DIRECT"
            )
            assert out == {"via": "executor"}  # flag-on 走治理链，不走直连

            async def _boom(a):
                raise RuntimeError("boom")

            ex.register_handler("p62_seam_probe", _boom)
            with pytest.raises(GatewayDenied) as exc_info:
                await run_governed("p62_seam_probe", {}, direct=lambda: "DIRECT")
            assert "boom" in str(exc_info.value)
        finally:
            tool_registry.delete_tool("p62_seam_probe")

    def test_bypass_allowlist_is_frozen_ratchet(self):
        """棘轮：KNOWN_BYPASS_TOOLS 只减不增；A2 本批 = 基线五项零变化。"""
        from agentix.tools.adapters.integrity import KNOWN_BYPASS_TOOLS

        assert set(KNOWN_BYPASS_TOOLS) <= set(BYPASS_BASELINE)
        assert set(KNOWN_BYPASS_TOOLS) == set(BYPASS_BASELINE)


class TestKgGatewayBridgeInstall:
    def test_flag_off_installs_nothing(self):
        from agentix.application.services.kg_gateway_bridge import (
            install_kg_gateway_handlers,
        )
        from agentix.tools.gateway.executor import get_tool_executor

        install_kg_gateway_handlers()
        ex = get_tool_executor()
        assert not any(ex.has_handler(name) for name in BYPASS_BASELINE)

    def test_flag_on_installs_all_five_and_audit_is_honest(self, monkeypatch):
        from agentix.application.services.kg_gateway_bridge import (
            install_kg_gateway_handlers,
        )
        from agentix.tools.gateway.executor import get_tool_executor
        from agentix.tools.adapters.integrity import audit_tool_registries

        monkeypatch.setattr(settings, "TOOL_GATEWAY_ENFORCED", True, raising=False)
        install_kg_gateway_handlers()
        install_kg_gateway_handlers()  # 幂等

        ex = get_tool_executor()
        for name in BYPASS_BASELINE:
            assert ex.has_handler(name), name

        audit = audit_tool_registries()
        assert audit.handler_no_spec == ()
        assert audit.ok
        # 装齐后豁免表亮起 = 接线真实存在的预期灰度信号（非漂移）
        assert set(audit.stale_exemptions) == set(BYPASS_BASELINE)


# ---------------------------------------------------------------------------
# A2c — 记忆三工具
# ---------------------------------------------------------------------------


class TestMemoryToolDeclarations:
    def test_flag_off_excludes_declarations_and_handlers(self):
        from agentix.application.services.memory_tools_bridge import (
            install_memory_tools,
        )
        from agentix.tools.gateway.executor import get_tool_executor

        registry_init._DECLARATIONS.clear()
        names = {spec.name for spec, _ in registry_init.get_declarations()}
        assert not set(MEMORY_TOOL_NAMES) & names

        install_memory_tools()
        ex = get_tool_executor()
        assert not any(ex.has_handler(name) for name in MEMORY_TOOL_NAMES)

    def test_flag_on_title_contract_risk_and_capabilities(self, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_TOOLS_ENABLED", True, raising=False)
        registry_init._DECLARATIONS.clear()
        decls = registry_init.get_declarations()
        by_name = {spec.name: (spec, cls) for spec, cls in decls}
        for name in MEMORY_TOOL_NAMES:
            assert name in by_name
            spec, cls = by_name[name]
            assert cls.model_json_schema()["title"] == name  # title 契约
            assert "memory" in spec.capabilities
        remember_spec = by_name["remember"][0]
        forget_spec = by_name["forget"][0]
        recall_spec = by_name["recall"][0]
        assert remember_spec.risk_level == "L1" and remember_spec.side_effect
        assert forget_spec.risk_level == "L1" and forget_spec.side_effect
        assert recall_spec.risk_level == "L0" and not recall_spec.side_effect


class TestMemoryToolRuntime:
    @pytest.fixture
    def installed_memory_tools(self, monkeypatch):
        """flag-on 安装三 handler + 单 provider（InMemory，不碰 Redis）。"""
        from agentix.agent.memory.service import InMemoryMemory
        from agentix.application.services import memory_manager as mm
        from agentix.application.services.memory_tools_bridge import (
            install_memory_tools,
        )

        monkeypatch.setattr(settings, "MEMORY_TOOLS_ENABLED", True, raising=False)
        registry_init._DECLARATIONS.clear()
        mm.reset_memory_manager()
        mm._manager = mm.MemoryManager([InMemoryMemory()])
        install_memory_tools()
        yield install_memory_tools
        mm.reset_memory_manager()

    @pytest.mark.asyncio
    async def test_remember_recall_forget_roundtrip(self, installed_memory_tools):
        from agentix.tools.gateway.executor import get_tool_executor

        ex = get_tool_executor()
        stored = await ex.execute(
            "remember",
            {"content": "P62 probe preference", "subject": "u1", "tags": ["probe"]},
        )
        assert stored.success, stored.error
        assert stored.data["status"] == "stored"

        found = await ex.execute("recall", {"query": "P62 probe", "subject": "u1"})
        assert found.success, found.error
        assert found.data["count"] >= 1
        item_id = found.data["items"][0]["item_id"]

        gone = await ex.execute("forget", {"item_id": item_id, "subject": "u1"})
        assert gone.success, gone.error
        assert gone.data["deleted"] >= 1

        after = await ex.execute("recall", {"query": "P62 probe", "subject": "u1"})
        assert after.success
        assert after.data["count"] == 0

    @pytest.mark.asyncio
    async def test_forget_contract_no_wildcard_and_miss_is_zero(self, installed_memory_tools):
        from agentix.tools.gateway.executor import get_tool_executor

        ex = get_tool_executor()
        # 缺匹配键 → ValueError → failed（不提供通配删除）
        bad = await ex.execute("forget", {"subject": "u1"})
        assert not bad.success
        assert "item_id or content" in (bad.error or "")
        # 未匹配 → 诚实 0（不抛不错报）
        miss = await ex.execute("forget", {"content": "definitely absent", "subject": "u1"})
        assert miss.success
        assert miss.data == {"status": "not_found", "deleted": 0}

    @pytest.mark.asyncio
    async def test_manager_forget_skips_providers_without_forget(self):
        from agentix.application.services.memory_manager import MemoryManager
        from agentix.domain.agent.types import MemoryItem

        class _NoForgetProvider:
            async def recall(self, *, scope, subject, query="", limit=10):
                return []

            async def remember(self, item: MemoryItem) -> str:
                return "id"

        manager = MemoryManager([_NoForgetProvider()])
        # duck-typed 扇出：未声明 forget 的 provider 静默跳过 → 0，不报错
        assert await manager.forget(scope="user", subject="u1", content="x") == 0
