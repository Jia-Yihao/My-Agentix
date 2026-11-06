"""领域层错误分类（P0-3）。

依据方案 [第5章]：故障按 API 层 / 工具层 / 上下文层 / 控制流层分类，各自兜底。
测试数据场景规范要求：用户不能把"系统降级兜底"当成测试数据答复——
因此错误必须结构化暴露（status/error_code），而非一律 200 + 中文文案。
"""

from enum import Enum


class ErrorCode(str, Enum):
    # API 层：上游模型服务问题
    LLM_TIMEOUT = "llm_timeout"
    LLM_RATE_LIMIT = "llm_rate_limit"
    LLM_UNAVAILABLE = "llm_unavailable"
    # 工具层：检索/图库/数据库
    TOOL_NOT_FOUND = "tool_not_found"
    TOOL_EXEC_FAILED = "tool_exec_failed"
    RETRIEVAL_FAILED = "retrieval_failed"
    GRAPH_UNAVAILABLE = "graph_unavailable"
    # 上下文层
    CONTEXT_OVERFLOW = "context_overflow"
    # DEAD-CODE(P2-5)｜处置=保留·预留语义｜控制流层（2026-09 审计）：**预留**，
    # 当前无产生点/消费点。与"死代码待删除"不同——本码属于文档化的对外契约面
    # （控制流层 → HTTP 500 映射表），
    # 错误码属 API 面，删除是破坏性接口变更；"预留但暂未产生"本身不是缺陷。
    # 建议接线点：图执行的步数上限检查（尚未实现）。标注保留以备追溯。
    MAX_STEPS_EXCEEDED = "max_steps_exceeded"
    # 控制流层：整图执行的墙钟超时（P0-2(a) 接线，AGENT_TIMEOUT 生效点）
    AGENT_TIMEOUT = "agent_timeout"
    # 控制流层：请求/子 Agent 的 token 预算已用尽（批次 13 接线，TOKEN_BUDGET_* 默认 0 ⇒ 不启用）
    # 语义上这是**控制决策**而非服务故障，故归入 DEGRADABLE（礼貌拒答），不进 HTTP_FATAL。
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"
    # 业务：合法但无结果（注意：这不是错误）
    NO_EVIDENCE = "no_evidence"
    OUT_OF_SCOPE = "out_of_scope"


# 可降级为「有礼貌的拒答」的错误（用户侧表现为"暂时无法回答"）
DEGRADABLE = {
    ErrorCode.LLM_TIMEOUT,
    ErrorCode.LLM_RATE_LIMIT,
    ErrorCode.LLM_UNAVAILABLE,
    ErrorCode.TOOL_EXEC_FAILED,
    ErrorCode.RETRIEVAL_FAILED,
    ErrorCode.GRAPH_UNAVAILABLE,
    ErrorCode.AGENT_TIMEOUT,
    ErrorCode.TOKEN_BUDGET_EXCEEDED,
}

# 应当暴露为 5xx / 4xx 而非 200 的错误
HTTP_FATAL = {
    ErrorCode.LLM_UNAVAILABLE: 503,
    ErrorCode.LLM_RATE_LIMIT: 429,
    ErrorCode.LLM_TIMEOUT: 504,
    ErrorCode.MAX_STEPS_EXCEEDED: 500,
    ErrorCode.AGENT_TIMEOUT: 504,
}


class AgentError(Exception):
    """所有 Agent 链路错误的统一载体。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        detail: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail  # 内部细节，仅在 DEBUG 下返回给客户端
        self.retryable = retryable
