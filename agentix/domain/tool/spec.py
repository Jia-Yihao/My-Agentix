"""ToolSpec / 风险分级契约（M0 拆入自 ``capabilities/tools/definitions.py`` 纯类型部分）。

零第三方依赖；``ToolRegistry``/注册表实现留在 ``capabilities/tools/definitions.py``
（旧路径 re-export，M6 清尾）。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Tuple

__all__ = [
    "ToolSpec",
    "RISK_LEVELS",
    "FORBIDDEN_RISK_LEVEL",
    "CONTROL_REQUIRED_FROM",
    "_strip_control_chars",
]

# 工具风险分级（技术变更与架构演进指南 v2.0 §2.1.3 / §2.1.5⑤）：
#   L0 只读——无额外控制（法条检索、图谱查询、text2sql 只读）
#   L1 可预览——生成后预览+可放弃（文书起草、检查单生成）
#   L2 需人工确认——必须走 HITL 审批（对外发送通知、提交材料）
#   L3 禁止——代码层禁止注册（自动立案、自动签署、冒充律师出具意见）
RISK_LEVELS = ("L0", "L1", "L2", "L3")

#: L3 是「禁止项」：不是"需要更严格的审批"，而是**根本不允许存在**。
#: 此前 `__post_init__` 只校验 `risk_level in RISK_LEVELS`，而 "L3" 本身
#: 就在 RISK_LEVELS 元组里 —— 于是上面这条「代码层禁止注册」的纪律
#: **从未被真正执行**（实测 `ToolSpec(risk_level="L3")` 构造成功）。
#: 现补上真实守卫，让文档纪律 = 可执行约束。
FORBIDDEN_RISK_LEVEL = "L3"

#: 需要显式声明控制措施的最低等级（§2.1.5⑤：L2 及以上）。
CONTROL_REQUIRED_FROM = 2


def _strip_control_chars(text: str) -> str:
    """剥离 C0/C1 控制字符（保留 \\n \\t）——MCP 描述当不可信输入。"""
    out: list[str] = []
    for ch in text:
        o = ord(ch)
        if ch in ("\n", "\t"):
            out.append(ch)
        elif o < 32 or (0x7F <= o <= 0x9F):
            continue
        else:
            out.append(ch)
    return "".join(out)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Describe a tool so it can be surfaced to LLMs.

    R3-2（Agent Runtime 架构 §16）：执行治理字段入 Spec——统一 ToolExecutor
    据此实施 timeout/retry，风险分级沿用 L0-L3（与文档 LOW/MEDIUM/HIGH/CRITICAL
    语义映射：L0≈LOW、L1/L2≈MEDIUM/HIGH、L3=CRITICAL 禁止注册）。
    """

    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    # 风险等级（默认 L0 只读）。L2 注册时必须填 control（下方字段），
    # __post_init__ 强制校验；L3 直接在代码层拒绝注册（禁止项）。
    risk_level: str = "L0"
    # 控制措施说明：L2 及以上**必填**（非空），否则拒绝注册（§2.1.5⑤）。
    # 用结构化字段而非在 description 里约定「控制：」小标题——按字符串匹配
    # 文档内容属于脆弱约束，且该概念本就该是可类型化、可断言的一等字段
    # （原 definitions.py 注释里提到的 `approval_hook` 从未存在过，这里是它的真实归宿）。
    control: str = ""
    # R3-2 执行治理字段（默认值保证既有声明零改动向后兼容）
    capabilities: Tuple[str, ...] = ()  # 能力标签（工具选择的 capability filter 维度）
    side_effect: bool = False  # 是否有副作用（写操作/外部世界变更）
    timeout_ms: int = 30_000  # 单次执行超时（毫秒）
    retryable: bool = False  # 失败是否可重试（只读工具才建议 True）
    max_retries: int = 2  # 最大重试次数（仅 retryable=True 时生效）

    def __post_init__(self) -> None:
        # 名称含控制字符（\n \r \x00 等）直接拒绝——MCP 远端工具投毒式命名
        # （书 §4.7）：名称会被拼进日志/路由/权限键，不可信输入必须在声明期挡住。
        if any(ord(c) < 32 or ord(c) == 127 for c in self.name):
            raise ValueError(
                f"Tool name {self.name!r} contains control characters "
                "(MCP untrusted tool names must be rejected at declaration)"
            )
        if self.risk_level not in RISK_LEVELS:
            raise ValueError(
                f"Tool '{self.name}' has invalid risk_level '{self.risk_level}' "
                f"(must be one of {RISK_LEVELS})"
            )
        if self.risk_level == FORBIDDEN_RISK_LEVEL:
            raise ValueError(
                f"Tool '{self.name}' declares forbidden risk_level '{FORBIDDEN_RISK_LEVEL}'. "
                "L3 是禁止项（自动立案/自动签署/冒充律师出具意见类能力），"
                "不允许在代码层注册——应重新设计为 L2 走人工审批，或不做。"
            )
        if self._rank >= CONTROL_REQUIRED_FROM and not self.control.strip():
            raise ValueError(
                f"Tool '{self.name}' declares risk_level '{self.risk_level}' but no "
                "`control` measure (must be a non-empty string). "
                "§2.1.5⑤：L2 及以上工具必须声明对应控制措施"
                "（例如 '需人工审批（HITL），无批准不得执行'）。"
            )
        if self.timeout_ms <= 0:
            raise ValueError(f"Tool '{self.name}' has invalid timeout_ms {self.timeout_ms} (must be > 0)")
        if self.max_retries < 0:
            raise ValueError(f"Tool '{self.name}' has invalid max_retries {self.max_retries} (must be >= 0)")
        if self.side_effect and self.retryable:
            raise ValueError(
                f"Tool '{self.name}' declares side_effect=True with retryable=True: "
                "side-effectful tools must not auto-retry (risk of duplicate writes)"
            )

    @property
    def _rank(self) -> int:
        """风险等级序号（L0=0 … L3=3）。"""
        return RISK_LEVELS.index(self.risk_level)

    def as_openai_function(self) -> Dict[str, Any]:
        """
        Return the spec formatted for OpenAI function calling.

        风险等级不进 LLM 可见描述——分级是 Harness 侧（Constrain 职责）的
        执行层约束，不是给模型的提示。

        描述侧防御（MCP 投毒，书 §4.7）：剥离 C0/C1 控制字符（保留换行/制表），
        防止远端工具描述携带不可见指令进入 prompt。
        """
        return {
            "name": self.name,
            "description": _strip_control_chars(self.description),
            "parameters": self.parameters,
        }

    def as_openai_tool(self) -> Dict[str, Any]:
        """
        Return the spec wrapped in the format expected by OpenAI `tools`.
        """
        return {
            "type": "function",
            "function": self.as_openai_function(),
        }
