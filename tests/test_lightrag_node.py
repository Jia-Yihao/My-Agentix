"""LightRAG 查询节点测试。

历史背景（为什么这个文件被重写）
--------------------------------
本文件原先是一个「名为 test、实为脚本」的探针：

1. 5 个 ``async def test_*`` 全部没有 ``@pytest.mark.asyncio``，在
   ``asyncio_mode=strict`` 下被 pytest 静默跳过（显示为 skipped，而非 failed），
   在「1430 passed / 7 skipped」的绿灯里长期隐身；
2. 零 ``assert``，只打日志，函数之间互相调用（``test_query`` 里调
   ``test_insert_document``），根本不是可独立运行的测试用例；
3. 把工作目录写进源码树（``./test_lightrag_data``），跑一次污染一次仓库；
4. 构造 ``LightRAGAPI(..., enable_neo4j=False)``——该参数早已从 ``__init__`` 移除，
   因此这 5 个函数**一次都没成功执行过**：一旦真正运行就是
   ``TypeError: unexpected keyword argument 'enable_neo4j'``。
   也就是说，「跳过」掩盖了一次 API 漂移。

重写后分两层，各自职责清晰：

* ``TestLightRAGQueryNodeContract`` —— 纯单元层，默认运行，零外部依赖。
  用 monkeypatch 把 LightRAG 挡在门外，只锁定 LangGraph 节点的**契约**：
  缺 task 时报错、下层异常被捕获而非向上抛。
* ``TestLightRAGEndToEnd`` —— 端到端层，``integration`` 标记 + 环境变量开关，
  需要可用的 OpenAI 兼容 LLM / Embedding 服务。默认跳过，且跳过原因是显式的，
  不会伪装成通过。
"""

import os
from unittest.mock import patch

import pytest

from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.customer_tools import (
    node as lightrag_node_module,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.customer_tools.node import (
    LIGHTRAG_AVAILABLE,
    LightRAGAPI,
    create_lightrag_query_node,
)

# 端到端开关：默认关闭。CI 不应因为它没配 LLM 而变红，也不该因为跳过而变绿。
RUN_LIGHTRAG_E2E = bool(os.getenv("LEGELBOT_RUN_LIGHTRAG_E2E"))

requires_lightrag = pytest.mark.skipif(
    not LIGHTRAG_AVAILABLE,
    reason="lightrag-hku 未安装（pip install lightrag-hku）",
)

requires_e2e_service = pytest.mark.skipif(
    not RUN_LIGHTRAG_E2E,
    reason=(
        "LightRAG 端到端测试需要可用的 LLM/Embedding 服务；"
        "设置 LEGELBOT_RUN_LIGHTRAG_E2E=1 以启用。"
    ),
)


# ---------------------------------------------------------------------------
# 第一层：纯单元契约
# ---------------------------------------------------------------------------


class TestLightRAGQueryNodeContract:
    """锁定 ``create_lightrag_query_node`` 的输入/输出契约，不触碰 LightRAG。"""

    class _FakeAPI:
        """最小 fake：只实现节点会调用的 ``query``。

        刻意不用 ``AsyncMock``——它的 ``return_value`` 会在 ``.get()`` 处
        静默产出未 await 的协程，把告警噪声带进测试输出。
        """

        def __init__(self, result=None, exc=None):
            self._result = result
            self._exc = exc

        async def query(self, query, mode=None):  # noqa: D401 - 与真实签名对齐
            if self._exc is not None:
                raise self._exc
            return self._result

    @pytest.mark.asyncio
    async def test_missing_task_reports_error_and_does_not_call_lightrag(self):
        """state 里没有 task 时，节点应记录错误，且不实例化 LightRAGAPI。"""
        node = create_lightrag_query_node()

        with patch.object(lightrag_node_module, "LightRAGAPI") as api_cls:
            result = await node({})

        api_cls.assert_not_called()
        assert result["steps"] == ["execute_lightrag_query"]
        assert len(result["cyphers"]) == 1
        output = result["cyphers"][0]
        assert "未提供查询文本" in output.errors
        assert output.records["result"] == ""

    @pytest.mark.asyncio
    async def test_query_failure_is_captured_not_raised(self):
        """底层查询抛异常时，节点必须捕获并写入 errors，绝不向上冒泡。

        这是 LangGraph 节点的基本纪律：一个子节点的失败不应炸穿整张图。
        """
        node = create_lightrag_query_node()
        fake = self._FakeAPI(exc=RuntimeError("embedding service down"))

        with patch.object(lightrag_node_module, "LightRAGAPI", return_value=fake):
            result = await node({"task": "民法典第1条的立法目的"})

        output = result["cyphers"][0]
        assert any("LightRAG 查询失败" in e for e in output.errors)
        assert any("embedding service down" in e for e in output.errors)
        assert output.records["result"] == ""

    @pytest.mark.asyncio
    async def test_successful_query_shape(self):
        """成功路径：records.result 承载 response，statement 带上检索模式。"""
        node = create_lightrag_query_node()
        fake = self._FakeAPI(
            result={
                "response": "民法典第1条明确了立法目的。",
                "mode": "hybrid",
                "query": "民法典第1条的立法目的",
            }
        )

        with patch.object(lightrag_node_module, "LightRAGAPI", return_value=fake):
            result = await node({"task": "民法典第1条的立法目的"})

        output = result["cyphers"][0]
        assert output.errors == []
        assert output.records["result"] == "民法典第1条明确了立法目的。"
        assert "hybrid" in output.statement
        assert output.task == "民法典第1条的立法目的"

    @pytest.mark.asyncio
    async def test_output_state_is_pydantic_validated(self):
        """返回的 cyphers 元素必须是可序列化的状态对象（LangGraph 会做 channel 合并）。"""
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.customer_tools.node import (
            LightRAGQueryOutputState,
        )

        node = create_lightrag_query_node()
        fake = self._FakeAPI(
            result={"response": "ok", "mode": "naive", "query": "t"}
        )

        with patch.object(lightrag_node_module, "LightRAGAPI", return_value=fake):
            result = await node({"task": "t"})

        assert isinstance(result["cyphers"][0], LightRAGQueryOutputState)
        # 能 dump 出来，说明字段类型都立得住
        assert isinstance(result["cyphers"][0].model_dump(), dict)


class TestLightRAGAPIConfigContract:
    """``LightRAGAPI`` 的配置优先级：显式参数 > settings 默认值。"""

    @requires_lightrag
    def test_explicit_params_override_settings(self):
        api = LightRAGAPI(
            working_dir="./tmp-x",
            retrieval_mode="local",
            top_k=3,
            max_token_size=512,
        )
        assert api.working_dir == "./tmp-x"
        assert api.retrieval_mode == "local"
        assert api.top_k == 3
        assert api.max_token_size == 512
        assert api.initialized is False

    @requires_lightrag
    def test_legacy_enable_neo4j_kwarg_is_gone(self):
        """回归守卫：``enable_neo4j`` 已从构造函数移除。

        旧测试脚本仍在传这个参数，导致 5 个「测试」一旦真跑就 TypeError。
        这里钉死它，避免将来有人照着旧脚本又把它写回去。
        """
        with pytest.raises(TypeError):
            LightRAGAPI(working_dir="./tmp-x", enable_neo4j=False)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# 第二层：端到端（默认跳过）
# ---------------------------------------------------------------------------


@requires_lightrag
@requires_e2e_service
class TestLightRAGEndToEnd:
    """真跑 LightRAG 的 insert → query。

    需要可用的 OpenAI 兼容端点（settings.OPENAI_API_BASE / OPENAI_API_KEY /
    EMBEDDING_MODEL）。运行方式：

        LEGELBOT_RUN_LIGHTRAG_E2E=1 pytest -m integration tests/test_lightrag_node.py

    工作目录落在 pytest 的 ``tmp_path``（每个用例独立临时目录），
    不再把数据写进源码树。
    """

    DOC = (
        "《民法典》第1条：为了保护民事主体的合法权益，调整民事关系，"
        "维护社会和经济秩序，适应中国特色社会主义发展要求，弘扬社会主义核心价值观，"
        "根据宪法，制定本法。"
    )

    @pytest.mark.asyncio
    async def test_insert_then_query(self, tmp_path):
        api = LightRAGAPI(
            working_dir=str(tmp_path / "lightrag"),
            retrieval_mode="hybrid",
            top_k=5,
        )
        await api.initialize()
        assert api.initialized is True

        inserted = await api.insert_documents([self.DOC])
        assert inserted["total"] == 1
        assert inserted["success"] == 1, f"插入失败: {inserted}"
        assert inserted["error"] == 0

        result = await api.query("民法典第1条的内容是什么？", mode="hybrid")
        assert result["mode"] == "hybrid"
        assert result["query"] == "民法典第1条的内容是什么？"
        assert isinstance(result["response"], str)
        assert result["response"].strip(), "LightRAG 返回了空响应"

    @pytest.mark.asyncio
    async def test_embedding_dimension_matches_settings(self, tmp_path):
        from agentix.config import settings

        api = LightRAGAPI(working_dir=str(tmp_path / "lightrag"))
        await api.initialize()

        vectors = await api.rag.embedding_func(["测试文本"])
        assert len(vectors) == 1
        assert len(vectors[0]) == int(settings.EMBEDDING_DIMENSION or 1536)
