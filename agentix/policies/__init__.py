"""横切策略层（L4）：Planner / Verifier / HITL / Guardrails 执行件
（Stage M·M3 自 ``agentix/agent/{planner,replanner,verifier,hitl}``、
``application/services/hitl_bridge.py`` 与 ``capabilities/tools/guardrails.py``
迁入）。

注意与 ``agentix/domain/policies/``（纯契约值对象）分界：本包只放**有实现**的
执行件。包 ``__init__`` 刻意零 import（策略件被 capabilities/orchestration 多方
引用，eager 汇总易踩半初始化窗口），符号请从子模块取。
"""
