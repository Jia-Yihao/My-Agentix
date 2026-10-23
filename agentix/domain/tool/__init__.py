"""Tool 契约包（M0 拆入：ToolSpec / Risk / Permission 纯类型）。

零第三方依赖；实现留在 ``capabilities/tools``（definitions/human_review）与
``application/services/hitl_bridge``（旧位置 re-export shim，M6 清尾）。
新代码请从本包导入。
"""

from agentix.domain.tool.permission import PermissionMode
from agentix.domain.tool.risk import RiskLevel
from agentix.domain.tool.sandbox import (
    SANDBOX_DEFAULT,
    SANDBOX_LADDER,
    ExecutionSandbox,
    Handler,
    SandboxUnavailable,
)
from agentix.domain.tool.spec import (
    CONTROL_REQUIRED_FROM,
    FORBIDDEN_RISK_LEVEL,
    RISK_LEVELS,
    ToolSpec,
)

__all__ = [
    "ToolSpec",
    "RISK_LEVELS",
    "FORBIDDEN_RISK_LEVEL",
    "CONTROL_REQUIRED_FROM",
    "RiskLevel",
    "PermissionMode",
    "ExecutionSandbox",
    "SandboxUnavailable",
    "SANDBOX_LADDER",
    "SANDBOX_DEFAULT",
    "Handler",
]
