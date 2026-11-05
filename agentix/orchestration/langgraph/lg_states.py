from pydantic import BaseModel, ConfigDict, Field
from dataclasses import dataclass, field
from typing import Annotated, Any, Dict, List, Literal, Optional
from langchain_core.messages import AnyMessage
from langgraph.graph import add_messages


class Router(BaseModel):
    """Classify user query (同时兼容旧字段/属性访问)."""

    logic: str = ""
    type: Literal[
        "general-query",
        "additional-query",
        "kb-query",
        "graphrag-query",
        "image-query",
        "file-query",
        "text2sql-query",
    ] = "kb-query"
    question: str = ""
    decision: Optional[str] = None
    confidence: Optional[float] = None
    reasoning: Optional[str] = None

    model_config = ConfigDict(extra = "allow")

    def get(self, key: str, default: Any = None) -> Any:
        """字典风格访问兼容旧逻辑。"""
        return getattr(self, key, self.__dict__.get(key, default))


class TaskRouter(Router):
    """P1（任务执行）：在 Router 白名单上追加 ``task-query`` 意图。

    仅当 ``settings.TASK_EXEC_ENABLED=true`` 时被 analyze 使用（结构化输出
    schema / 字面量校验都换成本类）；flag-off 时生产链路仍只构造基类
    ``Router``，其 ``type`` 字面量与基线**字节等价**——子类存在的意义是
    不动基类字段、不放宽既有 Literal，从而不惊动任何基类消费者。
    """

    type: Literal[
        "general-query",
        "additional-query",
        "kb-query",
        "graphrag-query",
        "image-query",
        "file-query",
        "text2sql-query",
        "task-query",
    ] = "kb-query"


class CodeRouter(Router):
    """P2（代码执行）：在 Router 白名单上追加 ``code-query`` 意图。

    与 :class:`TaskRouter` 同款不动基类字段、不放宽基类 ``type``
    字面量，只在**子类**上加一个取值。仅当 ``settings.CODE_EXEC_ENABLED=true``
    时被 analyze 使用（结构化输出 schema / 字面量校验都换成本类）；flag-off 时
    生产链路仍只构造基类 ``Router``，其 ``type`` 字面量与基线**字节等价**。
    """

    type: Literal[
        "general-query",
        "additional-query",
        "kb-query",
        "graphrag-query",
        "image-query",
        "file-query",
        "text2sql-query",
        "code-query",
    ] = "kb-query"


class TaskCodeRouter(TaskRouter):
    """两个 flag 同开时的联合白名单（task-query + code-query）。

    为什么不靠"运行时给 Literal 加值"：``Literal`` 是静态类型，schema 一旦
    按基类构造就固化了。联合白名单必须**显式声明**一份，否则两个 flag 同开时
    只能二选一（先命中的那个把另一个挡在白名单外）。
    """

    type: Literal[
        "general-query",
        "additional-query",
        "kb-query",
        "graphrag-query",
        "image-query",
        "file-query",
        "text2sql-query",
        "task-query",
        "code-query",
    ] = "kb-query"


class _KbChannelParams(BaseModel):
    """T30-2（单层路由）：KB 检索通道参数，语义照搬 A3 的 ``KBRouteDecision``。

    取值域与 ``kb_states.KBRouteDecision`` 的 ``route`` / ``tools`` 逐字一致
    （``local``/``external``/``hybrid`` × ``milvus``/``postgres``）——**不另立
    第三套通道语义**，否则钳制与安全边界要按两套词汇各写一遍。

    缺省值刻意是"没给参数"（``None`` / 空列表）而不是某个通道：兜底出口、
    fast-path、缓存命中都不产通道，此时 A3 照常自己决策（跳数回到基线）。
    """

    kb_route: Optional[Literal["local", "external", "hybrid"]] = None
    kb_tools: List[Literal["milvus", "postgres"]] = []


# T30-2 单层路由的四个变体：task/code 两闸的四种组合各一个，与既有四类一一对应。
#
# 为什么写成**显式子类**而不是 ``create_model`` 动态造类：
# 1. ``routing_cache.generation_token()`` 拿 ``__name__`` 当换代成分——类名必须
#    稳定可预期，显式声明让四个名字在文件里可直接读到，动态造名容易与令牌漂移；
# 2. 混入 ``_KbChannelParams`` 后 ``type`` 白名单**继承自对应基线类**，不必再抄
#    四份 8 项 ``Literal``（约定 6 ④：同一个判定不许两个源——白名单也一样）；
# 3. 结构化输出的 JSON schema 由字段声明决定，显式类对审查者是静态可读的。
class SingleLayerRouter(_KbChannelParams, Router):
    """单层路由基线变体：基线 7 类意图 + KB 通道参数。"""


class TaskSingleLayerRouter(_KbChannelParams, TaskRouter):
    """``TASK_EXEC_ENABLED`` 同开时的单层变体（追加 task-query）。"""


class CodeSingleLayerRouter(_KbChannelParams, CodeRouter):
    """``CODE_EXEC_ENABLED`` 同开时的单层变体（追加 code-query）。"""


class TaskCodeSingleLayerRouter(_KbChannelParams, TaskCodeRouter):
    """两个执行闸同开时的单层变体（追加 task-query + code-query）。"""


@dataclass(kw_only=True)
class RouteResult:
    """Route selection result for downstream nodes."""
    route: str
    confidence: float = 0.0
    next_node: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

class GradeHallucinations(BaseModel):
    """Binary score for hallucination present in generation answer."""

    binary_score: str = Field(
        description="Answer is grounded in the facts, '1' or '0'"
    )

@dataclass(kw_only=True)
class InputState:
    """Represents the input state for the agent.

    This class defines the structure of the input state, which includes
    the messages exchanged between the user and the agent. 
    """

    messages: Annotated[list[AnyMessage], add_messages]

    """Messages track the primary execution state of the agent.

    Typically accumulates a pattern of Human/AI/Human/AI messages; if
    you were to combine this template with a tool-calling ReAct agent pattern,
    it may look like this:

    1. HumanMessage - user input
    2. AIMessage with .tool_calls - agent picking tool(s) to use to collect
         information
    3. ToolMessage(s) - the responses (or errors) from the executed tools
    
        (... repeat steps 2 and 3 as needed ...)
    4. AIMessage without .tool_calls - agent responding in unstructured
        format to the user.

    5. HumanMessage - user responds with the next conversational turn.

        (... repeat steps 2-5 as needed ... )
    

    Merges two lists of messages, updating existing messages by ID.

    By default, this ensures the state is "append-only", unless the
    new message has the same ID as an existing message.
    

    Returns:
        A new list of messages with the messages from `right` merged into `left`.
        If a message in `right` has the same ID as a message in `left`, the
        message from `right` will replace the message from `left`."""


@dataclass(kw_only=True)
class AgentState(InputState):
    """State of the retrieval graph / agent."""
    router: Router = field(default_factory=lambda: Router(type="general-query", logic=""))
    """The router's classification of the user's query."""
    steps: list[str] = field(default_factory=list)
    """Populated by the retriever. This is a list of documents that the agent can reference."""
    documents: list[str] = field(default_factory=list)
    """Documents retrieved from the knowledge base."""
    question: str = field(default_factory=str)
    answer: str = field(default_factory=str)
    hallucination: GradeHallucinations = field(default_factory=lambda: GradeHallucinations(binary_score="0"))
    sources: list = field(default_factory=list)
    """Sources from knowledge base queries."""
    clarify_armed: bool = field(default_factory=lambda: False)
    """Phase 28（Ch9 §9.3.2）：本会话已发出澄清应答的单轮约束标记。

    经 checkpointer 跨轮持久；判定明确（无歧义）时由 handler 回写 False 恢复
    可澄清——保证「连续澄清至多 1 次，第二轮必须给出路由结果」（Ch5 禁再入循环）。
    """
