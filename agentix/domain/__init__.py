"""领域层：抽象接口（``ports``）、业务规则（``policies``）、错误与状态。

依赖规则（方案 2.2 规则 3）：``domain`` **不得**依赖任何 ``infrastructure`` /
``interfaces`` 符号。本包为**零外部依赖**层（仅 stdlib + typing）。

**P2-6 T7 变更**：历史上本包曾用 PEP 562 惰性导出 ``models``——一个 re-export
``interfaces.http.models`` 与 ``infrastructure.persistence.db.models`` 的兼容代理。
该代理违反规则 3，且全仓已无生产引用，故已按 P2-5 流程**删除**（连同其惰性
导出）。

**M0（统一执行链 Stage M）契约拆分**新增三个纯契约子包：

- ``agent``    —— Task/Plan/Observation/Verification/Memory 类型 + 七端口
  （自 ``agent_types.py`` 与 ``ports/agent_core.py`` 拆入）
- ``runtime``  —— Run/Step/Budget/Lifecycle 契约
- ``tool``     —— ToolSpec/Risk/Permission 契约（自 ``capabilities/tools``
  与 ``hitl_bridge`` 的纯类型拆入）

现子包全集：``errors`` / ``ports`` / ``policies`` / ``agent`` / ``runtime`` /
``tool``；顶层兼容模块 ``agent_types`` / ``status`` 保留为 re-export shim
（M6 清尾）。需要 ORM 实体请用 ``agentix.infrastructure.persistence.db.models``；
需要 HTTP 契约 schema 请用 ``agentix.interfaces.http.models``。
"""

__all__: list = []
