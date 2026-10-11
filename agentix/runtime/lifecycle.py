"""Agent 生命周期状态机（文档 §7）。

正常流与异常流都在这里集中约束，非法转换直接拒绝——防止各子图
自由改状态导致轨迹不可信。终态：COMPLETED / FAILED / CANCELLED。
"""

from __future__ import annotations

from agentix.runtime.state import AgentState, AgentStatus

#: 合法状态转换表（文档 §7：正常流 / 审批流 / 长任务 / 取消 / 失败）
ALLOWED_TRANSITIONS: dict[AgentStatus, frozenset[AgentStatus]] = {
    AgentStatus.CREATED: frozenset(
        {AgentStatus.RUNNING, AgentStatus.CANCELLED, AgentStatus.FAILED}
    ),
    AgentStatus.RUNNING: frozenset(
        {
            AgentStatus.PLANNING,
            AgentStatus.EXECUTING,
            AgentStatus.VERIFYING,
            AgentStatus.WAITING_APPROVAL,
            AgentStatus.PAUSED,
            AgentStatus.WAITING_EXTERNAL_EVENT,
            AgentStatus.COMPLETED,
            AgentStatus.FAILED,
            AgentStatus.CANCELLED,
        }
    ),
    AgentStatus.PLANNING: frozenset(
        {AgentStatus.EXECUTING, AgentStatus.FAILED, AgentStatus.CANCELLED}
    ),
    AgentStatus.EXECUTING: frozenset(
        {
            AgentStatus.VERIFYING,
            AgentStatus.EXECUTING,  # 多步执行期间保持 EXECUTING 合法（自环）
            AgentStatus.WAITING_APPROVAL,
            AgentStatus.PAUSED,
            AgentStatus.WAITING_EXTERNAL_EVENT,
            AgentStatus.FAILED,
            AgentStatus.CANCELLED,
        }
    ),
    AgentStatus.WAITING_APPROVAL: frozenset(
        {AgentStatus.RUNNING, AgentStatus.EXECUTING, AgentStatus.FAILED, AgentStatus.CANCELLED}
    ),
    AgentStatus.PAUSED: frozenset(
        {AgentStatus.RUNNING, AgentStatus.EXECUTING, AgentStatus.FAILED, AgentStatus.CANCELLED}
    ),
    AgentStatus.WAITING_EXTERNAL_EVENT: frozenset(
        {
            AgentStatus.RUNNING,
            AgentStatus.EXECUTING,
            AgentStatus.PAUSED,
            AgentStatus.FAILED,
            AgentStatus.CANCELLED,
        }
    ),
    AgentStatus.VERIFYING: frozenset(
        {AgentStatus.COMPLETED, AgentStatus.EXECUTING, AgentStatus.FAILED, AgentStatus.CANCELLED}
    ),
    # 终态：无出边
    AgentStatus.COMPLETED: frozenset(),
    AgentStatus.FAILED: frozenset(),
    AgentStatus.CANCELLED: frozenset(),
}


class InvalidTransitionError(Exception):
    """非法生命周期转换。"""

    def __init__(self, current: AgentStatus, target: AgentStatus):
        self.current = current
        self.target = target
        super().__init__(f"illegal agent status transition: {current.value} -> {target.value}")


class LifecycleManager:
    """AgentState.status 的唯一合法变更入口。"""

    @staticmethod
    def transition(state: AgentState, target: AgentStatus) -> AgentState:
        """校验并执行状态转换（返回同一 state 便于链式调用）。"""
        allowed = ALLOWED_TRANSITIONS[state.status]
        if target not in allowed:
            raise InvalidTransitionError(state.status, target)
        state.status = target
        return state

    @staticmethod
    def is_terminal(state: AgentState) -> bool:
        return not ALLOWED_TRANSITIONS[state.status]

    @staticmethod
    def fail(state: AgentState, code: str, message: str, *, stage: str | None = None,
             retryable: bool = False, detail: dict | None = None) -> AgentState:
        """便捷入口：转换到 FAILED 并写入结构化错误（终态不可再失败则原样返回）。"""
        from agentix.runtime.state import ErrorInfo

        if LifecycleManager.is_terminal(state):
            return state  # 已终态（如 CANCELLED）不覆盖既有结论
        LifecycleManager.transition(state, AgentStatus.FAILED)
        state.error = ErrorInfo(
            code=code, message=message, stage=stage, retryable=retryable, detail=detail or {}
        )
        return state
