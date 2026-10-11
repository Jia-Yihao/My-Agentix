"""Stage F2b-d（统一执行链 · R-P2-3 Graph 动态化）契约守卫。

钉死契约（DoD）：

1. **三键治理**：``KG_SCHEMA_AWARE_GENERATION_ENABLED`` /
   ``EVIDENCE_POOL_ENABLED`` / ``VERIFIER_GROUNDING_ENABLED`` 默认 False，
   且五处同步（settings / selfcheck / .env.example / configuration.md
   分组行+详情行双落 / p41 手写清单），rag-optimization 状态注记在案；
2. **schema 感知生成**：flag-off = 裸 ``graph.schema`` 逐位等价；flag-on =
   与校验同源的「法律域文档摘要 + 发现态 schema」渲染
   （``resolve_generation_schema``），渲染/读取异常 fail-open 回裸
   ``graph.schema``；生产链与孪生链接线为源级钉死；孪生链静态中文标签
   冗余块 flag 门控且 flag-off 渲染与基线拼接逐字节等价；
3. **证据池**：flag-off 观测 ``meta`` 无 ``evidence`` 键（与基线逐位等价）；
   flag-on 成功观测携带 ``{content ≤4000, source}``、失败观测不携带；
   与去重 stub 共存时证据取折叠前原文；``_extract_evidence`` 能消费该块；
4. **L3 总闸**：``task_execution`` 与 ``agent_loop_adapter`` 两处生产
   调用点读 ``VERIFIER_GROUNDING_ENABLED``（源级），默认关 = 基线
   ``require_grounding=False``；
5. **fast-path 不动**：预定义模板（``predefined_cypher``）保留；
   p68 的显式未实现契约（``_dynamic_classify``）未被本批破坏。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentix.application.agents.kg_sub_graph.agentic_rag_agents.retrievers.cypher_examples.legal_retriever import (
    LEGAL_GRAPH_SCHEMA,
    LegalCypherRetriever,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.text2cypher.generation.prompts import (
    create_text2cypher_generation_prompt_template,
    legacy_schema_hint,
)
from agentix.application.services.task_plan import TaskSpec as PipelineTaskSpec
from agentix.application.services.task_retry import TaskOutcome
from agentix.config import settings
from agentix.config.selfcheck import DEFAULT_IGNORED_MISSING
from agentix.policies.verifier.layered import LayeredVerifier

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_KEYS = (
    "KG_SCHEMA_AWARE_GENERATION_ENABLED",
    "EVIDENCE_POOL_ENABLED",
    "VERIFIER_GROUNDING_ENABLED",
)

_CYpher_TOOLS_UTILS = (
    PROJECT_ROOT
    / "agentix"
    / "application"
    / "agents"
    / "kg_sub_graph"
    / "agentic_rag_agents"
    / "components"
    / "cypher_tools"
    / "utils.py"
)
_TWIN_NODE = (
    PROJECT_ROOT
    / "agentix"
    / "application"
    / "agents"
    / "kg_sub_graph"
    / "agentic_rag_agents"
    / "components"
    / "text2cypher"
    / "generation"
    / "node.py"
)


def _read(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


class _FakeGraph:
    """最小图替身：裸 schema 与发现态 schema 可区分。"""

    schema = "RAW_GRAPH_SCHEMA"

    @property
    def get_schema(self) -> str:
        return "{n:Law(name STRING)}"


class _BrokenGraph(_FakeGraph):
    @property
    def get_schema(self) -> str:  # noqa: D401 - 故障替身
        raise RuntimeError("discovery down")


class TestKeyGovernance:
    def test_three_flags_default_false(self):
        assert settings.KG_SCHEMA_AWARE_GENERATION_ENABLED is False
        assert settings.EVIDENCE_POOL_ENABLED is False
        assert settings.VERIFIER_GROUNDING_ENABLED is False

    def test_selfcheck_allows_omission(self):
        for key in _KEYS:
            assert key in DEFAULT_IGNORED_MISSING, key

    def test_env_example_documents_all_three_as_active_lines(self):
        lines = _read(PROJECT_ROOT / ".env.example").splitlines()
        for key in _KEYS:
            assert any(
                line.strip() == f"{key}=false" for line in lines
            ), f"{key} 缺少 .env.example 活行"

    def test_configuration_doc_double_entry(self):
        text = _read(PROJECT_ROOT / "docs" / "architecture" / "configuration.md")
        for key in _KEYS:
            assert text.count(f"`{key}`") >= 2, f"{key} 需分组行+详情行双落"

    def test_p41_wiring_list_contains_keys(self):
        text = _read(PROJECT_ROOT / "tests" / "test_p41_doc_consistency.py")
        for key in _KEYS:
            assert f'"{key}"' in text, key

    def test_rag_optimization_annotation_present(self):
        text = _read(PROJECT_ROOT / "docs" / "design" / "rag-optimization.md")
        assert "R-P2-3 Graph 动态化已落地" in text
        assert "R-P2-5 混合检索归因报告已落地" in text
        assert "P2×5 全部落地" in text


class TestSchemaAwareGeneration:
    def test_flag_off_returns_raw_schema(self, monkeypatch):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.utils.utils import (
            resolve_generation_schema,
        )

        monkeypatch.setattr(
            settings, "KG_SCHEMA_AWARE_GENERATION_ENABLED", False
        )
        assert resolve_generation_schema(_FakeGraph()) == "RAW_GRAPH_SCHEMA"

    def test_flag_on_uses_discovered_render(self, monkeypatch):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.utils.utils import (
            resolve_generation_schema,
        )

        monkeypatch.setattr(settings, "KG_SCHEMA_AWARE_GENERATION_ENABLED", True)
        rendered = resolve_generation_schema(_FakeGraph())
        # 发现态 schema（花括号清洗为方括号，与校验链同源）
        assert "[n:Law(name STRING)]" in rendered
        # 非裸格式权威源
        assert rendered != "RAW_GRAPH_SCHEMA"
        # 法律域文档摘要前置（docs/design/legal_domain_schema.md 在仓）
        assert "Legal KG node types" in rendered

    def test_flag_on_render_failure_falls_back_to_raw(self, monkeypatch, caplog):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.utils.utils import (
            resolve_generation_schema,
        )

        monkeypatch.setattr(settings, "KG_SCHEMA_AWARE_GENERATION_ENABLED", True)
        with caplog.at_level("WARNING"):
            result = resolve_generation_schema(_BrokenGraph())
        assert result == "RAW_GRAPH_SCHEMA"
        assert any("fall back to raw graph.schema" in r.message for r in caplog.records)

    def test_flag_on_doc_missing_still_dynamic(self, monkeypatch):
        import agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.utils.utils as utils_mod
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.utils.utils import (
            resolve_generation_schema,
        )

        monkeypatch.setattr(settings, "KG_SCHEMA_AWARE_GENERATION_ENABLED", True)
        monkeypatch.setattr(utils_mod, "_format_legal_schema_from_docs", lambda: "")
        assert resolve_generation_schema(_FakeGraph()) == "[n:Law(name STRING)]"

    def test_production_node_wires_helper(self):
        src = _read(_CYpher_TOOLS_UTILS)
        assert "resolve_generation_schema(graph)" in src
        assert '"schema": graph.schema' not in src

    def test_twin_node_wires_helper_and_hint(self):
        src = _read(_TWIN_NODE)
        assert "resolve_generation_schema(graph)" in src
        assert "legacy_schema_hint()" in src
        assert '"schema": graph.schema' not in src


class TestTwinStaticBlockGate:
    def test_hint_flag_off_is_static_block(self, monkeypatch):
        monkeypatch.setattr(
            settings, "KG_SCHEMA_AWARE_GENERATION_ENABLED", False
        )
        assert legacy_schema_hint() == LEGAL_GRAPH_SCHEMA

    def test_hint_flag_on_is_empty(self, monkeypatch):
        monkeypatch.setattr(settings, "KG_SCHEMA_AWARE_GENERATION_ENABLED", True)
        assert legacy_schema_hint() == ""

    def test_flag_off_render_byte_identical_to_baseline_concat(self, monkeypatch):
        monkeypatch.setattr(
            settings, "KG_SCHEMA_AWARE_GENERATION_ENABLED", False
        )
        tpl = create_text2cypher_generation_prompt_template()
        text = tpl.format_messages(
            question="Q",
            fewshot_examples="F",
            schema="S",
            legacy_schema_hint=legacy_schema_hint(),
        )[1].content
        # 基线 = 模板前缀 + LEGAL_GRAPH_SCHEMA 全文 + 尾缀（拼接逐字节等价）
        baseline = (
            "静态 Schema 说明（冗余保险，动态 schema 以 S 为准）：\n"
            + LEGAL_GRAPH_SCHEMA
            + "\n\n                        用户输入: Q"
        )
        assert baseline in text

    def test_flag_on_render_has_no_static_block(self, monkeypatch):
        monkeypatch.setattr(settings, "KG_SCHEMA_AWARE_GENERATION_ENABLED", True)
        tpl = create_text2cypher_generation_prompt_template()
        text = tpl.format_messages(
            question="Q",
            fewshot_examples="F",
            schema="S",
            legacy_schema_hint=legacy_schema_hint(),
        )[1].content
        assert "你正在查询一个中国法律法规知识图谱" not in text
        # 动态 {schema} 仍在位
        assert "S" in text


class TestEvidencePool:
    @staticmethod
    def _spec() -> PipelineTaskSpec:
        return PipelineTaskSpec(name="w1", payload={"tool": "cypher_query"})

    def test_flag_off_meta_has_no_evidence_key(self, monkeypatch):
        from agentix.application.services.task_execution import _to_observation

        monkeypatch.setattr(settings, "EVIDENCE_POOL_ENABLED", False)
        outcome = TaskOutcome(
            name="w1", ok=True, value={"rows": [1]}, attempts=1
        )
        obs = _to_observation(self._spec(), outcome, wave=1)
        assert set(obs.meta) == {"tool", "attempts", "wave"}

    def test_flag_on_success_carries_evidence_block(self, monkeypatch):
        from agentix.application.services.task_execution import _to_observation

        monkeypatch.setattr(settings, "EVIDENCE_POOL_ENABLED", True)
        outcome = TaskOutcome(
            name="w1", ok=True, value={"rows": [1]}, attempts=1
        )
        obs = _to_observation(self._spec(), outcome, wave=1)
        blocks = obs.meta["evidence"]
        assert isinstance(blocks, list) and len(blocks) == 1
        assert blocks[0]["source"] == "cypher_query"
        assert blocks[0]["content"].startswith("{")
        assert '"rows"' in blocks[0]["content"]

    def test_flag_on_failure_carries_no_evidence(self, monkeypatch):
        from agentix.application.services.task_execution import _to_observation

        monkeypatch.setattr(settings, "EVIDENCE_POOL_ENABLED", True)
        outcome = TaskOutcome(
            name="w1", ok=False, error="boom", attempts=3
        )
        obs = _to_observation(self._spec(), outcome, wave=2)
        assert not obs.ok
        assert "evidence" not in obs.meta

    def test_flag_on_content_capped_at_4000(self, monkeypatch):
        from agentix.application.services.task_execution import _to_observation

        monkeypatch.setattr(settings, "EVIDENCE_POOL_ENABLED", True)
        outcome = TaskOutcome(
            name="w1", ok=True, value="x" * 10000, attempts=1
        )
        obs = _to_observation(self._spec(), outcome, wave=1)
        assert len(obs.meta["evidence"][0]["content"]) == 4000

    def test_flag_on_evidence_keeps_original_under_dedup_stub(
        self, monkeypatch
    ):
        from agentix.application.services.task_execution import _to_observation
        from agentix.tools.gateway.result_limiter import get_result_deduplicator

        monkeypatch.setattr(settings, "EVIDENCE_POOL_ENABLED", True)
        monkeypatch.setattr(settings, "TOOL_GUARDRAILS_ENABLED", True)
        dedup = get_result_deduplicator()
        value = {"rows": ["stable-payload"]}
        assert dedup.observe("cypher_query", value) is None
        assert dedup.observe("cypher_query", value) is None
        outcome = TaskOutcome(name="w1", ok=True, value=value, attempts=1)
        obs = _to_observation(self._spec(), outcome, wave=1)
        # 正文被折叠成 stub …
        assert obs.meta.get("deduped") is True
        assert "[重复结果已折叠]" in obs.content
        # … 但证据保留折叠前原文（grounding 不吃占位文本）
        assert '"stable-payload"' in obs.meta["evidence"][0]["content"]

    def test_extract_evidence_consumes_written_block(self, monkeypatch):
        from agentix.application.services.task_execution import _to_observation

        monkeypatch.setattr(settings, "EVIDENCE_POOL_ENABLED", True)
        outcome = TaskOutcome(
            name="w1", ok=True, value="第577条 当事人一方不履行合同义务…", attempts=1
        )
        obs = _to_observation(self._spec(), outcome, wave=1)
        pool = LayeredVerifier._extract_evidence([obs])
        assert len(pool) == 1
        assert "第577条" in pool[0]["content"]
        assert pool[0]["source"] == "cypher_query"

    def test_grounding_toggle_wired_at_both_call_sites(self):
        task_src = _read(
            PROJECT_ROOT / "agentix" / "application" / "services" / "task_execution.py"
        )
        adapter_src = _read(
            PROJECT_ROOT
            / "agentix"
            / "application"
            / "services"
            / "agent_loop_adapter.py"
        )
        for src in (task_src, adapter_src):
            assert "VERIFIER_GROUNDING_ENABLED" in src
            # 原硬编码调用形态不得复活
            assert "require_grounding=False, route=route" not in src
            assert "require_grounding=bool(" in src


class TestFastPathRetained:
    def test_predefined_templates_intact(self):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.predefined_cypher.legal_cypher_dict import (
            predefined_cypher_dict,
        )

        assert len(predefined_cypher_dict) >= 7
        for body in predefined_cypher_dict.values():
            assert "MATCH" in str(body)

    def test_dynamic_classify_contract_untouched(self):
        # p68 钉死的显式未实现契约（R-P2-3 裁定分类不在范围，保持不变）
        with pytest.raises(NotImplementedError, match="R-P0-4"):
            LegalCypherRetriever._dynamic_classify("合同违约相关法条有哪些")
