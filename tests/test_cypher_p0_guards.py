"""Cypher P0 注入防护测试。

覆盖《My Agentix_架构优化方案》P0 中 Cypher 安全四项修复：
1. WRITE_CLAUSES 黑名单补全（LOAD CSV / CALL / DROP / USE / IMPORT / APOC.）
2. 执行侧二次只读闸门（execution/node.py 拒绝执行写查询）
3. 末次尝试短路（attempt_cypher_execution_on_final_attempt 不放行写错误）
4. 标识符白名单（validators.is_valid_cypher_identifier）
"""

import pytest

from agentix.application.agents.kg_sub_graph.agentic_rag_agents.constants import (
    WRITE_CLAUSES,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2cypher.validation.validators import (
    is_valid_cypher_identifier,
    validate_no_writes_in_cypher_query,
)


class TestWriteClausesBlacklist:
    """黑名单补全：新增子句必须被 validate_no_writes_in_cypher_query 检出。"""

    @pytest.mark.parametrize(
        "clause",
        ["CREATE", "DELETE", "DETACH DELETE", "SET", "REMOVE", "FOREACH", "MERGE"],
    )
    def test_original_clauses_still_detected(self, clause):
        statement = f"MATCH (n) WHERE n.x = 'x' {clause} (n:Fake)"
        errors = validate_no_writes_in_cypher_query(statement)
        assert any(clause in e for e in errors)

    @pytest.mark.parametrize(
        "clause,statement",
        [
            ("LOAD CSV", "LOAD CSV WITH HEADERS FROM 'file:///x.csv' AS row RETURN row"),
            ("CALL", "CALL db.labels() YIELD label RETURN label"),
            ("CALL", "CALL { MATCH (n) RETURN n LIMIT 1 } RETURN n"),
            ("DROP", "DROP INDEX my_index IF EXISTS"),
            ("USE", "USE otherdb MATCH (n) RETURN n"),
            ("IMPORT", "IMPORT DATA FROM 'x'"),
            ("APOC.", "CALL apoc.periodic.iterate('MATCH (n) RETURN n', 'DELETE n', {}) YIELD batches RETURN batches"),
        ],
    )
    def test_new_clauses_detected(self, clause, statement):
        errors = validate_no_writes_in_cypher_query(statement)
        assert any(clause in e for e in errors), (
            f"{clause} should be detected in: {statement}"
        )

    def test_read_only_query_not_flagged(self):
        statement = "MATCH (n:Law)-[:HAS_ARTICLE]->(a) RETURN n.name, a.title LIMIT 10"
        assert validate_no_writes_in_cypher_query(statement) == []

    def test_constants_contains_new_entries(self):
        for clause in ("LOAD CSV", "CALL", "DROP", "USE", "IMPORT", "APOC."):
            assert clause in WRITE_CLAUSES


class TestIdentifierWhitelist:
    """LLM 输出标识符白名单：杜绝 f-string 插值注入。"""

    @pytest.mark.parametrize(
        "value",
        ["Law", "law_article", "_private", "A", "a1_2", "Z9"],
    )
    def test_valid_identifiers(self, value):
        assert is_valid_cypher_identifier(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "1abc",          # 数字开头
            "has space",     # 空格
            "a-b",           # 连字符
            "a; DROP",       # 注入片段
            "n`)",           # 反引号逃逸
            "",              # 空串
            "a--",           # 注释片段
            "a' OR 1=1",     # 引号注入
        ],
    )
    def test_invalid_identifiers(self, value):
        assert is_valid_cypher_identifier(value) is False

    def test_non_string_rejected(self):
        assert is_valid_cypher_identifier(None) is False
        assert is_valid_cypher_identifier(123) is False


class TestExecutionNodeWriteGate:
    """执行侧二次只读闸门：含写子句的语句不得触达 graph.query。"""

    @pytest.fixture
    def mock_graph(self):
        from unittest.mock import MagicMock

        g = MagicMock()
        g.query.return_value = [{"name": "test"}]
        return g

    def _make_node(self, mock_graph):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2cypher.execution.node import (
            create_text2cypher_execution_node,
        )
        return create_text2cypher_execution_node(mock_graph)

    @pytest.mark.asyncio
    async def test_blocks_write_query(self, mock_graph):
        node = self._make_node(mock_graph)
        state = {"statement": "MATCH (n) DETACH DELETE n", "errors": []}
        result = await node(state)

        mock_graph.query.assert_not_called()
        cypher_out = result["cyphers"][0]
        assert any("write clause" in str(e).lower() for e in cypher_out["errors"])

    @pytest.mark.asyncio
    async def test_allows_read_query(self, mock_graph):
        node = self._make_node(mock_graph)
        state = {"statement": "MATCH (n:Law) RETURN n LIMIT 1", "errors": []}
        result = await node(state)

        mock_graph.query.assert_called_once_with("MATCH (n:Law) RETURN n LIMIT 1")
        cypher_out = result["cyphers"][0]
        assert cypher_out["records"] == [{"name": "test"}]
        assert cypher_out["errors"] == []

    @pytest.mark.asyncio
    async def test_carries_existing_errors_through_gate(self, mock_graph):
        node = self._make_node(mock_graph)
        state = {
            "statement": "CREATE (n:Fake)",
            "errors": ["pre-existing error"],
        }
        result = await node(state)

        mock_graph.query.assert_not_called()
        cypher_out = result["cyphers"][0]
        assert "pre-existing error" in cypher_out["errors"]
        assert any("write clause" in str(e).lower() for e in cypher_out["errors"])

    @pytest.mark.asyncio
    async def test_apoc_write_blocked(self, mock_graph):
        node = self._make_node(mock_graph)
        state = {
            "statement": "CALL apoc.periodic.iterate('MATCH (n) RETURN n', 'DELETE n', {})",
            "errors": [],
        }
        await node(state)
        mock_graph.query.assert_not_called()


class TestValidationNodeFinalAttemptShortCircuit:
    """末次尝试短路：写错误即使 attempt_cypher_execution_on_final_attempt=True 也不放行。"""

    @pytest.fixture
    def mock_graph(self):
        from unittest.mock import MagicMock

        g = MagicMock()
        g.query.return_value = []  # EXPLAIN 调用
        return g

    def _make_node(self, mock_graph, max_attempts=3, final_attempt_exec=True):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2cypher.validation.node import (
            create_text2cypher_validation_node,
        )
        return create_text2cypher_validation_node(
            graph=mock_graph,
            llm=None,
            llm_validation=True,  # llm=None + llm_validation=True → 跳过 LLM/schema 校验分支
            max_attempts=max_attempts,
            attempt_cypher_execution_on_final_attempt=final_attempt_exec,
        )

    @pytest.mark.asyncio
    async def test_write_error_short_circuits_on_final_attempt(self, mock_graph):
        node = self._make_node(mock_graph)
        state = {"statement": "MATCH (n) DELETE n", "attempts": 2}
        result = await node(state)

        assert result["attempts"] == 3
        assert result["next_action_cypher"] == "__end__"
        assert any("write clause" in str(e).lower() for e in result["errors"])

    @pytest.mark.asyncio
    async def test_read_query_executes_on_final_attempt(self, mock_graph):
        # 无错误 + 末次尝试 + final_attempt_exec=True → 正常放行执行
        node = self._make_node(mock_graph)
        state = {"statement": "MATCH (n:Law) RETURN n LIMIT 1", "attempts": 2}
        result = await node(state)

        assert result["attempts"] == 3
        assert result["next_action_cypher"] == "execute_cypher"
        assert result["errors"] == []

    @pytest.mark.asyncio
    async def test_final_attempt_false_never_executes_with_errors(self, mock_graph):
        node = self._make_node(mock_graph, final_attempt_exec=False)
        state = {"statement": "MATCH (n) DELETE n", "attempts": 2}
        result = await node(state)

        assert result["next_action_cypher"] == "__end__"


class TestLlmFilterMappingRobustness:
    """LLM 输出过滤条件的健壮性（回归测试）。

    背景：`validate_cypher_query_with_llm` 里对 LLM 输出的 (node_label, property_key)
    做「字符串属性才做值映射」的判断时，原先写成

        graph.structured_schema["node_props"][filter.node_label]  # KeyError
        ... [0]["type"]                                           # IndexError

    上游只校验了标识符是否 *合法*（正则白名单），没有校验它是否 *存在于 schema*。
    因此当 LLM 幻觉出一个不存在的 label，或给已有 label 编了一个不存在的 property，
    校验节点会直接抛异常把整条 Text2Cypher 链路打断——而校验节点的职责恰恰是
    「把问题报告出来」，不是「被 LLM 输出击穿」。

    本类锁定修复后的行为：未知 label / 未知 property 一律降级为 mapping_error。
    """

    SCHEMA = {
        "node_props": {
            "Law": [
                {"property": "name", "type": "STRING"},
                {"property": "effective_year", "type": "INTEGER"},
            ],
            "Article": [{"property": "title", "type": "STRING"}],
        },
        "rel_props": {},
        "relationships": [],
    }

    @pytest.fixture
    def graph(self):
        from unittest.mock import MagicMock

        g = MagicMock()
        g.structured_schema = self.SCHEMA
        g.query.return_value = [{"yes": 1}]
        return g

    @staticmethod
    def _chain(filters):
        """构造一个只返回固定 ValidateCypherOutput 的假 chain。"""
        from unittest.mock import AsyncMock, MagicMock

        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2cypher.validation.models import (
            ValidateCypherOutput,
        )

        chain = MagicMock()
        chain.ainvoke = AsyncMock(
            return_value=ValidateCypherOutput(errors=None, filters=filters)
        )
        return chain

    @pytest.mark.asyncio
    async def test_unknown_label_records_mapping_error_instead_of_crashing(self, graph):
        """LLM 幻觉出一个 schema 里没有的 label -> 不得抛 KeyError。"""
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2cypher.validation.models import (
            Property,
        )
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2cypher.validation.validators import (
            validate_cypher_query_with_llm,
        )

        chain = self._chain(
            [Property(node_label="NotALabel", property_key="name", property_value="x")]
        )
        result = await validate_cypher_query_with_llm(
            validate_cypher_chain=chain,
            question="q",
            graph=graph,
            cypher_statement="MATCH (n:NotALabel) RETURN n",
        )

        assert result["errors"] == []
        assert any("Unknown node label" in e for e in result["mapping_errors"])
        graph.query.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_property_records_mapping_error_instead_of_crashing(
        self, graph
    ):
        """label 存在但 property 不存在 -> 不得抛 IndexError（空列表下标）。"""
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2cypher.validation.models import (
            Property,
        )
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2cypher.validation.validators import (
            validate_cypher_query_with_llm,
        )

        chain = self._chain(
            [Property(node_label="Law", property_key="not_a_prop", property_value="x")]
        )
        result = await validate_cypher_query_with_llm(
            validate_cypher_chain=chain,
            question="q",
            graph=graph,
            cypher_statement="MATCH (n:Law) RETURN n",
        )

        assert result["errors"] == []
        assert any("Unknown property" in e for e in result["mapping_errors"])
        graph.query.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_schema_does_not_crash(self):
        """structured_schema 缺 node_props 键时同样不得抛异常。"""
        from unittest.mock import MagicMock

        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2cypher.validation.models import (
            Property,
        )
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2cypher.validation.validators import (
            validate_cypher_query_with_llm,
        )

        g = MagicMock()
        g.structured_schema = {}
        chain = self._chain(
            [Property(node_label="Law", property_key="name", property_value="x")]
        )
        result = await validate_cypher_query_with_llm(
            validate_cypher_chain=chain,
            question="q",
            graph=g,
            cypher_statement="MATCH (n:Law) RETURN n",
        )
        assert any("Unknown node label" in e for e in result["mapping_errors"])

    @pytest.mark.asyncio
    async def test_non_string_property_is_skipped_without_query(self, graph):
        """非 STRING 属性不做值映射（原有语义保持不变）。

        注意 `effective_year` 是 INTEGER，且 `toLower()` 对数字无意义；
        修复前后都应「安静跳过」，既不发查询也不记 mapping_error。
        """
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2cypher.validation.models import (
            Property,
        )
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2cypher.validation.validators import (
            validate_cypher_query_with_llm,
        )

        chain = self._chain(
            [
                Property(
                    node_label="Law",
                    property_key="effective_year",
                    property_value="2020",
                )
            ]
        )
        result = await validate_cypher_query_with_llm(
            validate_cypher_chain=chain,
            question="q",
            graph=graph,
            cypher_statement="MATCH (n:Law) WHERE n.effective_year = 2020 RETURN n",
        )

        assert result["mapping_errors"] == []
        graph.query.assert_not_called()

    @pytest.mark.asyncio
    async def test_string_property_hit_produces_no_error(self, graph):
        """STRING 属性且库中存在该值 -> 无 mapping_error。"""
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2cypher.validation.models import (
            Property,
        )
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2cypher.validation.validators import (
            validate_cypher_query_with_llm,
        )

        chain = self._chain(
            [Property(node_label="Law", property_key="name", property_value="民法典")]
        )
        result = await validate_cypher_query_with_llm(
            validate_cypher_chain=chain,
            question="q",
            graph=graph,
            cypher_statement="MATCH (n:Law) WHERE n.name = '民法典' RETURN n",
        )

        assert result["mapping_errors"] == []
        graph.query.assert_called_once()

    @pytest.mark.asyncio
    async def test_string_property_miss_records_missing_value(self):
        """STRING 属性但库中查不到该值 -> 记录 Missing value mapping。"""
        from unittest.mock import MagicMock

        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2cypher.validation.models import (
            Property,
        )
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2cypher.validation.validators import (
            validate_cypher_query_with_llm,
        )

        g = MagicMock()
        g.structured_schema = self.SCHEMA
        g.query.return_value = []  # 查不到
        chain = self._chain(
            [Property(node_label="Law", property_key="name", property_value="不存在")]
        )
        result = await validate_cypher_query_with_llm(
            validate_cypher_chain=chain,
            question="q",
            graph=g,
            cypher_statement="MATCH (n:Law) WHERE n.name = '不存在' RETURN n",
        )

        assert any("Missing value mapping" in e for e in result["mapping_errors"])

    @pytest.mark.asyncio
    async def test_illegal_identifier_still_blocked_before_schema_lookup(self, graph):
        """白名单优先级不退化：非法标识符必须在 schema 查询之前就被拦下。"""
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2cypher.validation.models import (
            Property,
        )
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2cypher.validation.validators import (
            validate_cypher_query_with_llm,
        )

        chain = self._chain(
            [
                Property(
                    node_label="Law`) DETACH DELETE //",
                    property_key="name",
                    property_value="x",
                )
            ]
        )
        result = await validate_cypher_query_with_llm(
            validate_cypher_chain=chain,
            question="q",
            graph=graph,
            cypher_statement="MATCH (n:Law) RETURN n",
        )

        assert any("invalid identifier" in e for e in result["mapping_errors"])
        graph.query.assert_not_called()
