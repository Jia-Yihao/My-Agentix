"""审批权限模式契约（M0 拆入自 ``application/services/hitl_bridge.py`` 纯枚举）。"""

from enum import Enum

__all__ = [
    "PermissionMode",
]


class PermissionMode(str, Enum):
    """审批权限模式（Claude Code 多模式的简化子集，收敛在本桥配置面）。"""

    #: 拦下必批（挂起等人工裁决）——默认
    MANUAL = "default"
    #: 编辑类且无提权/无声明级参与 → 自动放行（留审计）
    ACCEPT_EDITS = "accept_edits"
    #: 操作员显式绕过 → 立即批准（留审计）
    BYPASS = "bypass"
