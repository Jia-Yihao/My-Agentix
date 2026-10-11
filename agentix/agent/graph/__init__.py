"""Graph 编排占位（LangGraph **仅**允许出现在本子包）。

Phase 2 不强制引入 LangGraph：主循环已是纯 Python AgentLoop。
若未来用 LangGraph 包一层，节点函数只调 agentix.agent.core 的 service。
"""

__all__: list = []
