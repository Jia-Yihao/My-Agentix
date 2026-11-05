"""R1-1：工具风险分级守卫测试。

依据：《My Agentix 技术变更与架构演进指南 v2.0》§2.1.3（执行工具风险分级表）
与 §2.1.5⑤（L2/L3 工具在代码层有拦截测试）。

锁定四条
1. 全部注册工具的 risk_level 合法（L0-L2；L3 为禁止项，见第 4 条）；
2. 当前声明式清单内的工具全部为 L0 只读（若未来引入 L1/L2 工具，
   此测试红——提示实现对应控制：L1 预览 / L2 HITL 审批）；
3. risk_level 不进 LLM 可见的 OpenAI function 描述（分级是 Harness 侧约束）；
4. L3（禁止项：自动立案/自动签署/冒充律师出具意见）在代码层不可注册。

⚠️ 修正记录：第 4 条原先只有文档纪律、没有可执行守卫。原
``test_l2_tool_registration_requires_approval_path`` 的 docstring 声称
"L2/L3 不得在无控制措施的情况下注册"，但断言用的是 ``risk_level="L9"``，
从未触达 L2/L3 —— 是**假绿**用例。现已拆成三个真实用例。
"""

from __future__ import annotations

import pytest

from agentix.tools.definitions import RISK_LEVELS, ToolSpec, registry
from agentix.tools.registry_init import ensure_registered, get_active_tools


def _all_registered_specs():
    ensure_registered()
    return list(registry.get_specs())


def test_all_registered_tools_have_valid_risk_level():
    specs = _all_registered_specs()
    assert specs, "registry 不应为空"
    for spec in specs:
        assert spec.risk_level in RISK_LEVELS, f"{spec.name}: 非法 risk_level"
        assert spec.risk_level != "L3", f"{spec.name}: L3 是禁止项，不允许出现在注册表里"


def test_declared_tools_are_all_l0_readonly():
    """当前 5 个工具（4 KG + search）均为只读 L0。

    引入 L1（可预览）/L2（需人工确认）工具时必须：
    L1 → 实现预览+放弃通道；L2 → 接入人工审批控制流（§4.12），
    并把本测试的断言同步更新为"分级与控制措施对应"。
    """
    specs = {spec.name: spec for spec in _all_registered_specs()}
    for name in (
        "cypher_query",
        "predefined_cypher",
        "microsoft_graphrag_query",
        "text2sql_query",
        "search",
    ):
        assert specs[name].risk_level == "L0", (
            f"{name} 应为 L0 只读；若已升级为有副作用的工具，请补对应控制措施"
        )


def test_invalid_risk_level_is_rejected():
    """非法等级（不在 RISK_LEVELS 内）必须被拒绝。"""
    with pytest.raises(ValueError, match="invalid risk_level"):
        ToolSpec(name="bad_tool", description="x", risk_level="L9")


def test_l3_is_forbidden_at_construction():
    """L3 是「代码层禁止注册」的禁止项，必须真的被拦住。

    ⚠️ 本用例是**修正**：原实现只在 ``__post_init__`` 里校验
    ``risk_level in RISK_LEVELS``，而 "L3" 本身就在 RISK_LEVELS 里 ——
    L3 因此在代码层**可以自由注册**，与 definitions.py 声明的
    「L3 禁止——代码层禁止注册（自动立案/自动签署/冒充律师出具意见）」
    直接矛盾。原守卫测试用 ``risk_level="L9"`` 断言，从未触达 L2/L3，
    属于**假绿**。现按真实纪律断言。
    """
    for forbidden in ("L3",):
        with pytest.raises(ValueError, match="forbidden risk_level"):
            ToolSpec(name="auto_sign_opinion", description="自动签署法律意见", risk_level=forbidden)

def test_l2_requires_control_measure():
    """L2 必须声明 control 控制措施（§2.1.5⑤），否则拒绝注册。

    用结构化 ``control`` 字段而非在 description 里匹配「控制：」小标题——
    按字符串匹配文档内容是脆弱约束，且该概念本就该是可类型化的一等字段。
    """
    with pytest.raises(ValueError, match="no `control` measure"):
        ToolSpec(name="send_notice", description="对外发送通知", risk_level="L2")

    # 只有空白也不算声明
    with pytest.raises(ValueError, match="no `control` measure"):
        ToolSpec(name="send_notice", description="对外发送通知", risk_level="L2", control="   ")

    ok = ToolSpec(
        name="send_notice",
        description="对外发送通知",
        risk_level="L2",
        side_effect=True,
        control="需人工审批（HITL），无批准不得执行",
    )
    assert ok.risk_level == "L2"
    # L0/L1 不要求 control（既有声明零改动）
    assert ToolSpec(name="cypher_query", description="只读查询").control == ""


def test_l3_takes_precedence_over_control_requirement():
    """L3 直接拒绝注册——不应因为 control 缺失而报"更弱的"那个错。"""
    with pytest.raises(ValueError, match="forbidden risk_level"):
        ToolSpec(
            name="auto_sign_opinion",
            description="自动签署法律意见",
            risk_level="L3",
            control="随便写点什么",
        )


def test_control_not_exposed_to_llm():
    """control 与 risk_level 一样属于 Harness 侧约束，不进 LLM 可见描述。"""
    spec = ToolSpec(
        name="send_notice",
        description="对外发送通知",
        risk_level="L2",
        control="需人工审批（HITL）",
    )
    fn = spec.as_openai_function()
    assert set(fn.keys()) == {"name", "description", "parameters"}
    assert "control" not in fn and "risk_level" not in fn


def test_risk_level_not_exposed_to_llm():
    """risk_level 不进 OpenAI function 描述（Constrain 属 Harness 职责）。"""
    spec = ToolSpec(name="demo", description="d", risk_level="L0")
    fn = spec.as_openai_function()

    assert "risk_level" not in fn
    assert set(fn.keys()) == {"name", "description", "parameters"}


def test_active_tools_respect_disabled_list_with_risk():
    """DISABLED_TOOLS 裁剪后返回的 schema 与 spec 分级一致（元数据不丢）。"""
    from agentix.config import settings

    ensure_registered()
    active = get_active_tools()
    assert active  # 默认全启用
    disabled = {
        name.strip() for name in (settings.DISABLED_TOOLS or "").split(",") if name.strip()
    }
    specs = {spec.name: spec for spec in _all_registered_specs()}
    for name, spec in specs.items():
        if name not in disabled:
            assert spec.risk_level == "L0"
