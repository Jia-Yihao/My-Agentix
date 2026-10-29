"""统一编排层（L2）：唯一推进权所在（Stage M·M2 自 ``agentix/agent`` 与
``agentix/application/agents/lg_*`` 迁入）。

- ``orchestrator`` — AgentLoop（自 agent/core/loop.py 迁入）
- ``executor``     — MappingStepExecutor（自 agent/executor/ 迁入）
- ``langgraph``    — LangGraph adapter/builders（自 application/agents/lg_* 迁入；
  ``lg_prompts`` 业务 prompt 留 application，由 builders 上行引用）
- ``strategies``   — multi_agent 策略件（自 agent/multiagent/ 迁入）

包 ``__init__`` 刻意零 import（lg_builder ↔ lg_supervisor 存在模块级互引，
且经 ``agentix.agent`` 的回引需惰性窗口），符号请从子模块取。
（2026-10-29 M3-5b 返工更正：括注内「lg_builder ↔ lg_supervisor 模块级互引」
一面已消失——lg_builder 侧本就是函数体内延迟 import，lg_supervisor 侧的顶层
lg_builder import 也已下移到 ``build_supervisor_graph()`` 调用体内；「刻意零
import」维持，另一面 ``agentix.agent`` 回引的惰性窗口依旧，且 M3-5b 起
lg_builder 的 import 期要求装配根已加载，eager 导出同样会把装配欠账转嫁给
本包的 importer。）
"""
