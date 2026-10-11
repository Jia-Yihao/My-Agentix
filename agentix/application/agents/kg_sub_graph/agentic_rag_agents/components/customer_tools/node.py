"""
LightRAG 查询节点（R-P0-1：平行实现已收敛至单实现）

本文件曾维护一套与 ``agentix/application/services/lightrag_service.py``
平行的 LightRAG 实现（embedding / 查询 / 插入），存在维度与凭据双源漂移。
Stage B2（R-P0-1）收敛后唯一实现在 ``LightRAGService``（embedding dim 三级
推断 / EMBEDDING_* 专用凭据 / 流响应包装）；本文件只保留 LangGraph 节点
``create_lightrag_query_node`` 与兼容门面 ``LightRAGAPI``
（历史调用方：kg_gateway_bridge、scripts/init_lightrag.py）。
"""
from typing import Any, Callable, Coroutine, Dict, List, Optional

from pydantic import BaseModel, Field

try:
    import lightrag.kg.shared_storage  # noqa: F401
    import lightrag.llm.openai  # noqa: F401
    import lightrag.utils  # noqa: F401
    LIGHTRAG_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    LIGHTRAG_AVAILABLE = False

# 导入配置
from agentix.tools.gateway.kg_gateway import run_governed
from agentix.config import settings
from agentix.infrastructure.core.logger import get_logger

logger = get_logger(service="lightrag-node")


#  LightRAG 查询的输入状态类型
class LightRAGQueryInputState(BaseModel):
    task: str
    query: str
    steps: List[str] = Field(...)


#   LightRAG 查询的输出状态类型
class LightRAGQueryOutputState(BaseModel):
    task: str
    query: str
    statement: str = Field(default="")
    parameters: str = Field(default="")
    errors: List[str] = Field(...)
    records: Dict[str, Any] = Field(...)
    steps: List[str] = Field(...)


#  LightRAG API（兼容门面；唯一实现在 lightrag_service）
class LightRAGAPI:
    """LightRAG API 兼容门面（R-P0-1：Strangler 转调层）。

    实现已收敛至 ``agentix/application/services/lightrag_service.py``
    （单实现），本类只保留历史兼容外观：

    - 构造签名与属性 ``working_dir/retrieval_mode/top_k/max_token_size``
      不变（显式参数 > settings 默认）；
    - ``initialize()`` 幂等，失败抛 ``Exception("LightRAG 初始化失败: ...")``；
    - ``query()`` 返回 ``{"response", "mode", "query"}`` 字典；
    - ``insert_documents()`` 返回 ``{"total", "success", "error"}`` 字典
      （``error`` 为失败计数，对应单实现响应的 ``failed``）；
    - ``self.rag`` 指向底层 LightRAG 实例（初始化后可用）；
    - ``enable_neo4j`` 构造参数保持移除（见 tests/test_lightrag_node.py 守卫）。

    行为契约由 tests/test_lightrag_node.py 与
    tests/test_p68_rag_p0_convergence.py 钉住。转调层与历史行为形状全兼容
    （无旧行为可回退），故不新增回退 flag。
    """

    def __init__(
        self,
        working_dir: Optional[str] = None,
        retrieval_mode: Optional[str] = None,
        top_k: Optional[int] = None,
        max_token_size: Optional[int] = None,
    ):
        """
        初始化 LightRAG API 门面

        Parameters
        ----------
        working_dir : str, optional
            LightRAG 工作目录
        retrieval_mode : str, optional
            检索模式: local, global, hybrid, naive, mix, bypass
        top_k : int, optional
            返回的 top-k 结果数量
        max_token_size : int, optional
            文本单元的最大 token 数量
        ----
        LightRAG 使用自己的文件存储系统（NetworkX + JSON），
        不使用 Neo4j 作为存储后端。Neo4j 是独立的结构化知识图谱。
        """
        if not LIGHTRAG_AVAILABLE:
            raise ImportError("LightRAG 未安装，请运行 'pip install lightrag-hku'")

        # 从环境变量获取配置，如果提供了参数则使用参数值
        self.working_dir = working_dir or settings.LIGHTRAG_WORKING_DIR
        self.retrieval_mode = retrieval_mode or settings.LIGHTRAG_RETRIEVAL_MODE
        self.top_k = top_k or settings.LIGHTRAG_TOP_K
        self.max_token_size = max_token_size or settings.LIGHTRAG_MAX_TOKEN_SIZE

        self._svc: Optional[Any] = None
        self.rag: Optional[Any] = None
        self.initialized = False

    def _get_service(self) -> Any:
        """按需构造唯一实现（延迟导入，实例复用）。

        延迟导入保证未安装 lightrag-hku 时本模块仍可被安全导入
        （``LIGHTRAG_AVAILABLE`` 软守卫在构造函数处兜底）。
        """
        if self._svc is None:
            from agentix.application.services.lightrag_service import (
                LightRAGService,
            )

            self._svc = LightRAGService(
                working_dir=self.working_dir,
                default_mode=self.retrieval_mode,
                default_top_k=self.top_k,
                max_token_size=self.max_token_size,
            )
        return self._svc

    async def initialize(self) -> None:
        """初始化底层唯一实现（幂等）；错误信息沿历史前缀由单实现给出。"""
        await self._get_service().initialize()
        self.rag = self._get_service().rag
        self.initialized = True

    async def query(self, query: str, mode: Optional[str] = None) -> Dict[str, Any]:
        """执行查询，返回历史 dict 契约 ``{"response", "mode", "query"}``。"""
        await self.initialize()
        retrieval_mode = mode or self.retrieval_mode
        response = await self._get_service().query(
            query, mode=retrieval_mode, top_k=self.top_k, stream=False
        )
        return {
            "response": response,
            "mode": retrieval_mode,
            "query": query,
        }

    async def insert_documents(self, documents: List[str]) -> Dict[str, Any]:
        """批量插入，返回历史 dict 契约 ``{"total", "success", "error"}``。"""
        await self.initialize()
        result = await self._get_service().insert_documents(documents)
        return {
            "total": result.total,
            "success": result.success,
            "error": result.failed,
        }


def create_lightrag_query_node(
) -> Callable[
    [Dict[str, Any]],
    Coroutine[Any, Any, Dict[str, List[LightRAGQueryOutputState] | List[str]]],
]:
    """
    创建 LightRAG 查询节点，用于 LangGraph 工作流
    Returns
    -------
    Callable[[Dict[str, Any]], Dict[str, List[LightRAGQueryOutputState] | List[str]]]
        名为 `lightrag_query` 的 LangGraph 节点
    """

    async def lightrag_query(
        state: Dict[str, Any],
    ) -> Dict[str, List[LightRAGQueryOutputState] | List[str]]:
        """
        执行 LightRAG 查询并返回结果
        """
        errors = []
        search_result = {}

        # 获取查询文本
        query = state.get("task", "")
        if not query:
            errors.append("未提供查询文本")
        else:
            try:
                # 创建 LightRAG API 实例
                lightrag_api = LightRAGAPI()

                # 调用 LightRAG API 获取数据
                search_result = await run_governed(
                    "microsoft_graphrag_query",
                    {"query": query},
                    direct=lambda: lightrag_api.query(query),
                )

            except Exception as e:
                errors.append(f"LightRAG 查询失败: {str(e)}")
                logger.error(f"LightRAG 查询节点执行失败: {str(e)}", exc_info=True)

        return {
            "cyphers": [
                LightRAGQueryOutputState(
                    **{
                        "task": state.get("task", ""),
                        "query": query,
                        "statement": f"LightRAG {search_result.get('mode', '')} search",
                        "parameters": "",
                        "errors": errors,
                        "records": {"result": search_result.get("response", "")},
                        "steps": ["execute_lightrag_query"],
                    }
                )
            ],
            "steps": ["execute_lightrag_query"],
        }

    return lightrag_query

create_graphrag_query_node = create_lightrag_query_node
GraphRAGAPI = LightRAGAPI
