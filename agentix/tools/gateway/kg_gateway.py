"""KG/检索类工具执行缝（A2b，``TOOL_GATEWAY_ENFORCED``）。

守卫 #2（v2 §6）「所有 ToolCall 必经 Tool Gateway」的收编层：``search`` /
``cypher_query`` / ``predefined_cypher`` / ``microsoft_graphrag_query`` /
``text2sql_query`` 五项历史上在 LangGraph 节点内直连执行（``integrity.
KNOWN_BYPASS_TOOLS`` 署名在案）。本模块提供六处调用点共用的统一执行缝::

    flag-off（默认）        → ``direct()`` 直连，与基线逐位等价；
    flag-on + handler 已装  → ``ToolExecutor.execute`` 治理链
                              （有效风险 → 权限 → HITL → 超时 → 重试 → 轨迹）；
    flag-on + handler 缺失  → 告警后回落直连（接线缺失不打挂节点）。

handler 由组合根 ``kg_gateway_bridge.install_kg_gateway_handlers()`` 安装；
``KNOWN_BYPASS_TOOLS`` 保持五项不变——flag-off 默认路径仍旁路，豁免表必须
继续说真话（flag-on 装齐后审计的 ``stale_exemptions`` 亮起属预期灰度信号）。
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

from loguru import logger

from agentix.config import settings

#: 直连回调：``() -> Any``（同步结果或 awaitable 均可，语义与节点原代码一致）
DirectCall = Callable[[], Any]


def gateway_routed(tool_name: str) -> bool:
    """该工具本次是否改走治理链（flag-on 且 handler 已安装）。

    flag-off 第一行即返回——不 import executor、零副作用，与基线逐位等价。
    """
    if not bool(getattr(settings, "TOOL_GATEWAY_ENFORCED", False)):
        return False
    from agentix.tools.gateway.executor import get_tool_executor

    executor = get_tool_executor()
    if not executor.has_handler(tool_name):
        logger.warning(
            "TOOL_GATEWAY_ENFORCED=true but no handler for {}; direct execution (bypass)",
            tool_name,
        )
        return False
    return True


class GatewayDenied(RuntimeError):
    """治理链拒绝或失败（flag-on 时向外传播，节点按既有 except 语义处理）。"""

    def __init__(self, tool_name: str, error: str | None):
        super().__init__(f"{tool_name} denied/failed via tool gateway: {error}")
        self.tool_name = tool_name
        self.error = error


async def run_governed(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    direct: DirectCall,
) -> Any:
    """执行一次工具调用（A2b 执行缝，六处调用点共用）。

    - flag-off（默认）：调用 ``direct()`` 并按需 await——行为与节点直连
      逐位等价（仅多一层协程封装，无 I/O 与状态差异）；
    - flag-on：经 ``ToolExecutor.execute``；失败（权限/风险/HITL 拒绝、
      超时、handler 故障）抛 :class:`GatewayDenied`——节点既有 ``except``
      分支把它当普通执行失败处理，错误语义不变，只是前面多了治理层。
    """
    if not gateway_routed(tool_name):
        outcome = direct()
        if inspect.isawaitable(outcome):
            outcome = await outcome
        return outcome
    from agentix.tools.gateway.executor import get_tool_executor

    result = await get_tool_executor().execute(tool_name, arguments)
    if not result.success:
        raise GatewayDenied(tool_name, result.error)
    return result.data
