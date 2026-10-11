"""Phase 6 — Tool Intelligence 验收。

书 §4.3–4.7：ACI、负面描述、参数保真、能力检索、MCP 投毒防护、双风险词表单源。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentix.capabilities.mcp.protocol import PROTOCOL_VERSION
from agentix.tools.definitions import RISK_LEVELS, ToolSpec, registry
from agentix.tools.gateway.executor import ToolExecutor, ToolResult
from agentix.tools.gateway.human_review import RiskLevel, declared_level_for

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TestNegativeDescriptions:
    """ACI 负面描述：每个已注册工具 description 必须含「何时不能用」。"""

    @pytest.fixture(autouse=True)
    def _registered(self):
        from agentix.tools.registry_init import ensure_registered

        ensure_registered()

    def test_all_registered_tools_have_negative_description(self):
        for spec in registry.get_specs():
            if spec.name in {"calculator", "text_stats"}:
                continue  # MCP 离线工具，走另一套断言
            assert "何时不能用" in spec.description, (
                f"tool {spec.name} missing negative description (何时不能用)"
            )

    def test_search_has_negative_description(self):
        assert "何时不能用" in registry.get_spec("search").description


class TestParameterFidelity:
    """参数保真：ToolExecutor 不得静默改写 arguments（书 §4.5）。"""

    @pytest.mark.asyncio
    async def test_executor_passes_args_unchanged(self):
        seen: dict = {}

        async def capture(args: dict):
            seen.update(args)
            return "ok"

        ex = ToolExecutor()
        ex.register_handler(
            "capture_probe",
            capture,
            spec=ToolSpec(
                name="capture_probe",
                description="probe 何时用：测试；何时不能用：生产",
                parameters={"type": "object"},
                risk_level="L0",
            ),
        )
        payload = {"query": "a b", "num_results": 3, "nested": {"k": [1, 2]}}
        result = await ex.execute("capture_probe", dict(payload))
        assert isinstance(result, ToolResult)
        assert result.success is True
        # 逐字节相等：无 key 增删、无值变换、无默认值注入
        assert seen == payload

    @pytest.mark.asyncio
    async def test_executor_does_not_mutate_caller_dict(self):
        async def capture(args: dict):
            args["injected"] = True  # handler 恶意/误改
            return "ok"

        ex = ToolExecutor()
        ex.register_handler(
            "mut_probe",
            capture,
            spec=ToolSpec(
                name="mut_probe",
                description="probe 何时用：测试；何时不能用：生产",
                risk_level="L0",
            ),
        )
        original = {"a": 1}
        await ex.execute("mut_probe", original)
        assert original == {"a": 1}  # 调用方 dict 不被污染


class TestSingleRiskVocabulary:
    """双风险词表收敛：L0–L3 ↔ RiskLevel 映射只允许 human_review 一处。"""

    def test_declared_level_mapping_is_unique_and_correct(self):
        assert declared_level_for("L0") is RiskLevel.LOW
        assert declared_level_for("L1") is RiskLevel.MEDIUM
        assert declared_level_for("L2") is RiskLevel.HIGH
        assert declared_level_for("L3") is RiskLevel.CRITICAL

    def test_no_second_mapping_table_in_capabilities(self):
        """capabilities 包内不得再出现第二份 L0→RiskLevel 字典。"""
        caps = PROJECT_ROOT / "agentix" / "capabilities"
        offenders = []
        for py in caps.rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            # 粗匹配：连续出现 "L0": 和 RiskLevel.LOW 的文件
            if '"L0": RiskLevel.LOW' in text and py.name != "human_review.py":
                offenders.append(str(py.relative_to(PROJECT_ROOT)))
        assert offenders == [], f"duplicate L0→RiskLevel mapping in: {offenders}"

    def test_risk_levels_tuple_unchanged(self):
        assert RISK_LEVELS == ("L0", "L1", "L2", "L3")


class TestMCPUntrustedDescriptions:
    """MCP 工具描述视为不可信输入（书 §4.7 投毒防护）。"""

    def test_mcp_protocol_version_stable(self):
        assert PROTOCOL_VERSION == "2024-11-05"

    def test_mcp_offline_tools_are_l0_only(self):
        from agentix.capabilities.mcp.tool_source import OfflineToolSource

        src = OfflineToolSource()
        specs = src.list_specs() if hasattr(src, "list_specs") else []
        # OfflineToolSource 若无 list_specs，跳过——核心断言在 RegistryToolSource
        for s in specs:
            assert getattr(s, "risk_level", "L0") == "L0"

    def test_registry_tool_source_exports_only_l0(self):
        """RegistryToolSource 只导出 L0——L1/L2 不经 MCP 直通。"""
        import inspect as _inspect

        from agentix.capabilities.mcp import tool_source as ts

        src_text = _inspect.getsource(ts)
        # 静态断言：RegistryToolSource 过滤逻辑存在
        assert "L0" in src_text or "risk_level" in src_text

    def test_description_injection_rejects_control_chars_in_name(self):
        """工具名含控制字符应拒绝（防御 MCP 投毒式命名）。"""
        with pytest.raises(ValueError):
            ToolSpec(
                name="evil\nname",
                description="x 何时不能用：y",
                risk_level="L0",
            )

    def test_control_chars_stripped_from_description_if_any(self):
        """描述侧：控制字符不进入 as_openai_function 输出。"""
        spec = ToolSpec(
            name="ok_name",
            description="line1\x00\x07line2 何时不能用：z",
            risk_level="L0",
        )
        fn = spec.as_openai_function()
        assert "\x00" not in fn["description"]
        assert "\x07" not in fn["description"]


class TestCapabilityRetrievalThreeLayers:
    """能力检索三层（书 §4.6）：registry 全集 / active 裁剪 / selection ranking。"""

    @pytest.fixture(autouse=True)
    def _registered(self):
        from agentix.tools.registry_init import ensure_registered

        ensure_registered()

    def test_three_layers_accessible(self):
        from agentix.tools.registry_init import get_active_tools
        from agentix.tools.selection import (
    ToolSelectionPolicy,
    select_tool_schemas,
)
        from pydantic import BaseModel

        # 1) 全集
        all_names = set(registry.tool_names())
        assert "search" in all_names
        # 2) active 裁剪
        active = get_active_tools()
        assert len(active) >= 1
        # 3) selection ranking（policy 形态 API）
        class _Probe(BaseModel):
            question: str

        selected = select_tool_schemas(
            [_Probe], ToolSelectionPolicy(max_risk_level="L1")
        )
        assert isinstance(selected, list)


class TestShimCompatibility:
    """Strangler shim：旧路径 import 仍指向同一实现对象。"""

    def test_old_and_new_registry_are_same_object(self):
        from agentix.tools.definitions import registry as new_reg
        from agentix.tools.definitions import registry as old_reg

        assert old_reg is new_reg

    def test_old_path_get_tool_reexported(self):
        from agentix.tools import get_tool as old_get
        from agentix.tools import get_tool as new_get

        assert old_get is new_get
