"""P2 注册表对账（借鉴 hermes ``dispatch`` 契约，source-code-study §5 2.1）。

三张表各有生命周期、靠隐式约定耦合——割裂处就是漂移处：

1. **声明清单** ``_DECLARATIONS``（``registry_init``）：LLM 可见 schema 的唯一数据源；
2. **spec 注册表** ``definitions.registry``：name → ToolSpec/factory（治理元数据）；
3. **handler 表** ``ToolExecutor._handlers``：name → 可调用实现。

本模块只做**只读审计**（不注册、不修改任何表），把漂移分类报告：

- ``declared_no_handler``：声明在册却没有 handler ⇒ 调了必 ``unknown tool``
  （**预期豁免除外**：LangGraph 节点直连执行的工具、组件内部直连的工具，
  逐项落在 ``KNOWN_BYPASS_TOOLS`` 并写明理由）；
- ``handler_no_spec``：handler 在册却没有 spec ⇒ 执行期过不了 spec 闸门；
- ``dynamic_handlers``：spec 在册但不在声明清单（MCP 动态导入等，信息项）；
- ``stale_exemptions``：豁免项**已可执行**（信息项——接线补齐后豁免该撤）。

``ok = not declared_no_handler and not handler_no_spec``。
组合根 ``main._startup`` 每次启动跑一遍：只告警、不阻断（诊断只读契约，
对齐 hermes ``/context`` 只读纪律）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Sequence, Tuple

from agentix.tools.definitions import ToolSpec, registry as tool_registry

__all__ = [
    "KNOWN_BYPASS_TOOLS",
    "RegistryAudit",
    "audit_tool_registries",
]

#: 有意不走 executor 的工具——**逐项写明理由**，豁免必须可回答「为什么」。
#: 对账健康不是「条目越少越好」，而是「每个缺口都有署名的解释」。
#: A2b（``TOOL_GATEWAY_ENFORCED``）：本集合**保持五项不变**——flag-off（默认）
#: 节点仍直连，豁免必须继续说真话；flag-on 装齐 handler 后 ``stale_exemptions``
#: 亮起属预期灰度信号（接线真实存在的证据），不是漂移。
KNOWN_BYPASS_TOOLS: Dict[str, str] = {
    "search": (
        "SearchTool 在 kb_tools 节点直接实例化（检索域组件内部调用，非 LLM "
        "工具面）；executor.execute('search') 从未被生产调用"
    ),
    "cypher_query": "KG 子图节点直接执行（LangGraph 工具调用路径暂不经 ToolExecutor）",
    "predefined_cypher": "KG 子图节点直接执行（LangGraph 工具调用路径暂不经 ToolExecutor）",
    "microsoft_graphrag_query": "KG 子图节点直接执行（LangGraph 工具调用路径暂不经 ToolExecutor）",
    "text2sql_query": "KG 子图节点直接执行（LangGraph 工具调用路径暂不经 ToolExecutor）",
}


@dataclass(frozen=True)
class RegistryAudit:
    """三表对账结果（只读快照；字段保持确定性排序，便于对拍）。"""

    declared_no_handler: Tuple[str, ...] = ()
    handler_no_spec: Tuple[str, ...] = ()
    dynamic_handlers: Tuple[str, ...] = ()
    stale_exemptions: Tuple[str, ...] = ()
    bypass: Mapping[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.declared_no_handler and not self.handler_no_spec

    def summary(self) -> str:
        return (
            f"declared_no_handler={list(self.declared_no_handler)} "
            f"handler_no_spec={list(self.handler_no_spec)} "
            f"dynamic_handlers={list(self.dynamic_handlers)} "
            f"stale_exemptions={list(self.stale_exemptions)}"
        )


def audit_tool_registries(
    *,
    executor: Optional[object] = None,
    declarations: Optional[Sequence[Tuple[ToolSpec, type]]] = None,
) -> RegistryAudit:
    """对账三表（只读；任何失败降级为带诊断信息的报告，不抛）。

    :param executor: 默认取进程单例 ``get_tool_executor()``（测试可注入）。
    :param declarations: 默认取声明清单公共视图 ``get_declarations()``。
    """
    if declarations is None:
        from agentix.tools.registry_init import get_declarations

        declarations = get_declarations()
    if executor is None:
        from agentix.tools.gateway.executor import get_tool_executor

        executor = get_tool_executor()

    declared_names = tuple(spec.name for spec, _ in declarations)
    declared_set = set(declared_names)
    handler_names = tuple(sorted(executor.handler_names()))

    declared_no_handler = tuple(
        name
        for name in declared_names
        if not executor.has_handler(name) and name not in KNOWN_BYPASS_TOOLS
    )
    handler_no_spec = tuple(
        name for name in handler_names if not tool_registry.has_tool(name)
    )
    dynamic_handlers = tuple(
        name for name in handler_names if name not in declared_set
    )
    stale_exemptions = tuple(
        name for name in sorted(KNOWN_BYPASS_TOOLS) if executor.has_handler(name)
    )
    return RegistryAudit(
        declared_no_handler=declared_no_handler,
        handler_no_spec=handler_no_spec,
        dynamic_handlers=dynamic_handlers,
        stale_exemptions=stale_exemptions,
        bypass=dict(KNOWN_BYPASS_TOOLS),
    )
