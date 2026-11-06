"""KB 多工具工作流的状态模型与运行时配置（P1-1 拆分）。

从 multi_tool.py 搬移，供 kb_nodes/ 与 multi_tool.py 共享（避免循环 import）。
"""

from typing import Any, Dict, List, Optional, Literal

from dataclasses import dataclass
from operator import add

try:  # pragma: no cover - prefer typing_extensions for Pydantic compatibility
    from typing_extensions import Annotated, NotRequired, TypedDict  # type: ignore
except ImportError:  # pragma: no cover - minimal stdlib fallback
    from typing import Annotated, NotRequired, TypedDict

from pydantic import BaseModel, Field


class KBGuardrailsDecision(BaseModel):
    decision: Literal["proceed", "end"]
    summary: Optional[str] = None
    rationale: Optional[str] = None


class KBRouteDecision(BaseModel):
    route: Literal["local", "external", "hybrid"]
    rationale: str
    tools: List[Literal["milvus", "postgres"]] = Field(
        description="本地知识源检索工具列表，支持 milvus/postgres",
    )


class KBInputState(TypedDict):
    """KB 子图的**输入面**（``StateGraph(..., input_schema=KBInputState)`` 按它过滤入参）。

    T30-2（单层路由）多出的两个键是 **``NotRequired``**：L1 给出了检索通道时，
    ``create_kb_query`` 才把它们一并传进来（A3 据此跳过模型调用）；不传时它们
    根本不在入参 dict 里，A3 照常自己决策。
    这两个键**必须**在这里声明——框架只把输入 schema 认识的键写进状态通道，
    只在 ``KBWorkflowState`` 上有键是不够的：实测入参里的 ``route``/``kb_tools``
    会被输入面**静默丢弃**（不报错），单层化就只剩"多一段提示词"。
    """

    question: str
    history: List[Dict[str, str]]
    route: NotRequired[str]
    kb_tools: NotRequired[List[str]]


class KBWorkflowState(TypedDict):
    question: str
    history: List[Dict[str, str]]
    guardrails_decision: str
    summary: str
    route: str
    kb_tools: List[str]
    milvus_results: List[Dict[str, Any]]
    postgres_results: List[Dict[str, Any]]
    local_results: List[Dict[str, Any]]
    external_results: List[Dict[str, Any]]
    answer: str
    degraded: bool
    steps: Annotated[List[str], add]
    sources: Annotated[List[str], add]


class KBOutputState(TypedDict):
    answer: str
    steps: List[str]
    sources: List[str]
    degraded: bool


@dataclass(kw_only=True)
class KBWorkflowConfig:
    """KB 工作流运行时配置（装配层解析 settings 后一次性打包，节点工厂注入）。

    仅含"影响节点行为的值"，llm / knowledge_service 等运行时对象
    由各节点工厂单独注入，不参与配置序列化。
    """

    top_k: int
    similarity_threshold: Optional[float] = None
    postgres_search_url: Optional[str] = None
    external_url: Optional[str] = None
    allow_external_search: bool = False
    external_is_postgres: bool = False
    request_timeout: float = 30.0
    filter_expr: Optional[str] = None
    scope_text: str = ""
