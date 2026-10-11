"""KB 多工具工作流的状态模型与运行时配置（P1-1 拆分）。

从 multi_tool.py 搬移，供 kb_nodes/ 与 multi_tool.py 共享（避免循环 import）。
"""

from typing import Any, Dict, List, Optional, Literal

from dataclasses import dataclass
from operator import add

try:  # pragma: no cover - prefer typing_extensions for Pydantic compatibility
    from typing_extensions import Annotated, TypedDict  # type: ignore
except ImportError:  # pragma: no cover - minimal stdlib fallback
    from typing import Annotated, TypedDict

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
    question: str
    history: List[Dict[str, str]]


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
