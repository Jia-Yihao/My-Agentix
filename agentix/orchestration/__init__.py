"""统一编排层（L2）：唯一推进权所在（Stage M·M2 自 ``agentix/agent`` 与
``agentix/application/agents/lg_*`` 迁入）。

- ``orchestrator`` — AgentLoop（自 agent/core/loop.py 迁入）
- ``executor``     — MappingStepExecutor（自 agent/executor/ 迁入）
- ``langgraph``    — LangGraph adapter/builders（自 application/agents/lg_* 迁入；
  ``lg_prompts`` 业务 prompt 留 application，由 builders 上行引用）
- ``strategies``   — multi_agent 策略件（自 agent/multiagent/ 迁入）

包 ``__init__`` 刻意零 import（lg_builder ↔ lg_supervisor 存在模块级互引，
且经 ``agentix.agent`` 的回引需惰性窗口），符号请从子模块取。
"""
