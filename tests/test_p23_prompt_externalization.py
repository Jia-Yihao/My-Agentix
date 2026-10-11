"""P2-3 提示词外置 + 静态前缀排序验收测试（全离线）。

覆盖 4 个站点（对应架构审计给出的内联提示词位置）：

1. ``components/planner/prompts.py`` —— human 模板外置为 ``PLANNER_HUMAN_PROMPT``
2. ``application/agents/guardrails.py`` —— 范围/图谱前缀外置到 ``lg_prompts``，
   且动态 ``logic`` 从系统消息中移出（静态前缀在前）
3. ``components/predefined_cypher/utils.py`` —— 参数提取提示外置到同包 ``prompts.py``
4. ``application/services/task_handlers.py`` —— 会话摘要提示外置到 ``services/prompts.py``

不依赖 LLM / Redis / Neo4j / Milvus / Docker。
"""

from pathlib import Path

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.prompts.chat import (
    HumanMessagePromptTemplate,
    SystemMessagePromptTemplate,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_PLANNER_PKG = "agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.planner"
_PREDEFINED_PKG = (
    "agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.predefined_cypher"
)


def _src(rel: str) -> str:
    return (PROJECT_ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 站点 1：planner human 模板
# ---------------------------------------------------------------------------


class TestPlannerPromptExternalized:
    REL = (
        "agentix/application/agents/kg_sub_graph/agentic_rag_agents/"
        "components/planner/prompts.py"
    )
    NODE = (
        "agentix/application/agents/kg_sub_graph/agentic_rag_agents/"
        "components/planner/node.py"
    )
    _FRAGMENT = "确保任务不会返回重复或相似的信息"

    def test_constant_importable_and_nonempty(self):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.planner.prompts import (
            PLANNER_HUMAN_PROMPT,
        )

        assert PLANNER_HUMAN_PROMPT.strip()
        assert self._FRAGMENT in PLANNER_HUMAN_PROMPT

    def test_literal_no_longer_inside_function_body(self):
        # 外置后，模板字面量应当只在模块级常量里，函数体内不再内联。
        import inspect

        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.planner import (
            prompts as planner_prompts,
        )

        fn_src = inspect.getsource(planner_prompts.create_planner_prompt_template)
        assert self._FRAGMENT not in fn_src
        assert "PLANNER_HUMAN_PROMPT" in fn_src

    def test_node_file_has_no_inline_prompt(self):
        node_src = _src(self.NODE)
        assert self._FRAGMENT not in node_src
        assert "create_planner_prompt_template" in node_src

    def test_static_system_message_first(self):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.planner.prompts import (
            create_planner_prompt_template,
        )
        from agentix.application.agents.kg_sub_graph.prompts.kg_prompts import (
            PLANNER_SYSTEM_PROMPT,
        )

        template = create_planner_prompt_template()
        assert isinstance(template.messages[0], SystemMessagePromptTemplate)
        assert isinstance(template.messages[1], HumanMessagePromptTemplate)
        assert template.messages[0].prompt.template == PLANNER_SYSTEM_PROMPT
        # 动态问题占位只出现在随后的 human 消息里
        assert "{question}" not in template.messages[0].prompt.template
        assert "{question}" in template.messages[1].prompt.template


# ---------------------------------------------------------------------------
# 站点 2：guardrails 前缀外置 + 动态 logic 移出系统消息
# ---------------------------------------------------------------------------


class TestGuardrailsPromptExternalized:
    REL = "agentix/application/agents/guardrails.py"

    def test_prefix_literals_moved_out_of_guardrails(self):
        guardrails_src = _src(self.REL)
        assert "参考此范围描述来决策" not in guardrails_src
        assert "参考图表结构来回答" not in guardrails_src

    def test_prefix_constants_importable(self):
        from agentix.application.agents import lg_prompts

        assert lg_prompts.GUARDRAILS_SCOPE_PREFIX.strip()
        assert lg_prompts.GUARDRAILS_GRAPH_PREFIX.strip()
        assert "{question}" in lg_prompts.GUARDRAILS_QUESTION_SUFFIX
        assert "参考此范围描述来决策" in lg_prompts.GUARDRAILS_SCOPE_PREFIX
        assert "参考图表结构来回答" in lg_prompts.GUARDRAILS_GRAPH_PREFIX

    def test_logic_block_extracted_from_static_system_prompt(self):
        from agentix.application.agents import lg_prompts

        static = lg_prompts.GET_ADDITIONAL_SYSTEM_PROMPT
        dynamic = lg_prompts.GET_ADDITIONAL_LOGIC_PROMPT
        # 系统消息必须是纯静态：不再含动态插值占位
        assert static.strip()
        assert "{logic}" not in static
        # 动态分类理由拆到后续消息
        assert "{logic}" in dynamic
        assert dynamic.format(logic="<REASON>").find("<REASON>") != -1

    def test_messages_static_system_first(self):
        import inspect

        from agentix.application.agents import guardrails as guardrails_module

        src = inspect.getsource(guardrails_module.get_additional_info)
        # 静态系统消息先于动态理由消息出现在 messages 构造中
        assert src.index("GET_ADDITIONAL_SYSTEM_PROMPT") < src.index(
            "GET_ADDITIONAL_LOGIC_PROMPT"
        )
        # 系统消息本身不被 format 插值
        assert "GET_ADDITIONAL_SYSTEM_PROMPT.format" not in src

    def test_streaming_config_contract_unchanged(self):
        """P1-4 真流式契约：本次改动不得改变 config 转发规则。"""
        import inspect

        from agentix.application.agents import guardrails as guardrails_module

        src = inspect.getsource(guardrails_module.get_additional_info)
        # 面向用户的回答调用仍然转发 config
        assert "model.ainvoke(messages, config=config)" in src
        # 内部判定链仍然刻意不转发 config
        chain_line = next(
            line for line in src.splitlines() if "guardrails_chain.ainvoke(" in line
        )
        assert "config=config" not in chain_line


# ---------------------------------------------------------------------------
# 站点 3：predefined_cypher 参数提取提示
# ---------------------------------------------------------------------------


class _CapturingLLM:
    """记录最后一次 invoke 收到的 PromptValue，并返回固定 JSON 内容。"""

    def __init__(self) -> None:
        self.last_prompt = None

    def invoke(self, prompt_value):
        self.last_prompt = prompt_value

        class _Resp:
            content = '{"条号": "1"}'

        return _Resp()


class TestParamExtractionPromptExternalized:
    REL = (
        "agentix/application/agents/kg_sub_graph/agentic_rag_agents/"
        "components/predefined_cypher/utils.py"
    )
    _FRAGMENT = "你是参数提取助手"

    def test_literal_moved_out_of_utils(self):
        assert self._FRAGMENT not in _src(self.REL)

    def test_constants_importable_and_nonempty(self):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.predefined_cypher import (
            prompts,
        )

        assert prompts.PARAM_EXTRACTION_SYSTEM_PROMPT.strip()
        assert self._FRAGMENT in prompts.PARAM_EXTRACTION_SYSTEM_PROMPT
        human = prompts.PARAM_EXTRACTION_HUMAN_PROMPT
        assert human.strip()
        assert "{user_question}" in human
        assert "{query_name}" in human
        assert "{param_names}" in human

    def test_static_system_message_first_offline(self):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.predefined_cypher import (
            prompts,
        )

        template = ChatPromptTemplate.from_messages(
            [
                ("system", prompts.PARAM_EXTRACTION_SYSTEM_PROMPT),
                ("human", prompts.PARAM_EXTRACTION_HUMAN_PROMPT),
            ]
        )
        assert isinstance(template.messages[0], SystemMessagePromptTemplate)
        assert isinstance(template.messages[1], HumanMessagePromptTemplate)
        assert template.messages[0].prompt.template == (
            prompts.PARAM_EXTRACTION_SYSTEM_PROMPT
        )
        # 动态占位只在 human 消息
        for placeholder in ("{user_question}", "{query_name}", "{param_names}"):
            assert placeholder not in template.messages[0].prompt.template

    def test_rendered_messages_static_before_dynamic(self):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.predefined_cypher.utils import (
            VectorQueryMatcher,
        )
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.predefined_cypher import (
            prompts,
        )

        llm = _CapturingLLM()
        result = VectorQueryMatcher._extract_parameters_with_llm(
            "民法典第1条怎么规定的", ["法规名", "条号"], "article_by_no", llm
        )
        assert result == {"条号": "1"}

        messages = llm.last_prompt.to_messages()
        assert messages[0].content == prompts.PARAM_EXTRACTION_SYSTEM_PROMPT
        assert "民法典第1条怎么规定的" in messages[1].content
        assert "article_by_no" in messages[1].content
        assert "法规名" in messages[1].content


# ---------------------------------------------------------------------------
# 站点 4：services 会话摘要提示
# ---------------------------------------------------------------------------


class TestConversationSummaryPromptExternalized:
    REL = "agentix/application/services/task_handlers.py"
    _FRAGMENT = "请用2-3句话总结以下对话的核心内容和用户关注点"

    def test_literal_moved_out_of_handler(self):
        handler_src = _src(self.REL)
        assert self._FRAGMENT not in handler_src
        assert "CONVERSATION_SUMMARY_PROMPT" in handler_src

    def test_constant_importable_and_nonempty(self):
        from agentix.application.services import prompts

        assert prompts.CONVERSATION_SUMMARY_PROMPT.strip()
        assert self._FRAGMENT in prompts.CONVERSATION_SUMMARY_PROMPT
        assert "{conversation_text}" in prompts.CONVERSATION_SUMMARY_PROMPT

    def test_static_prefix_before_dynamic(self):
        from agentix.application.services import prompts

        template = prompts.CONVERSATION_SUMMARY_PROMPT
        assert template.index(self._FRAGMENT) < template.index("{conversation_text}")

        rendered = template.format(conversation_text="用户: 你好")
        assert rendered.index(self._FRAGMENT) < rendered.index("用户: 你好")
        assert rendered.endswith("用户: 你好")


# ---------------------------------------------------------------------------
# 站点 5（批次 7）：重复前缀串合并 —— 单一事实源守卫
#
# 三个站点共享同一对中文前缀字面量：
#   lg_prompts.GUARDRAILS_SCOPE_PREFIX / GUARDRAILS_GRAPH_PREFIX（唯一事实源）
#   1. components/guardrails/prompts.py:31,38（活代码，逐字节一致复用）
#   2. kg_sub_graph/prompts/schema_utils.py:61,68（唯一调用方 ps_genai_agents
#      为遗留死代码；历史拼接尾部 "\n\n" 且 graph 前缀无前导换行，
#      为不改变 prompt 字节，用 [1:] 去掉常量自带的前导 "\n" 保持原样）
# ---------------------------------------------------------------------------


class TestPromptPrefixSingleSource:
    GUARDRAILS_PROMPTS = (
        "agentix/application/agents/kg_sub_graph/agentic_rag_agents/"
        "components/guardrails/prompts.py"
    )
    SCHEMA_UTILS = "agentix/application/agents/kg_sub_graph/prompts/schema_utils.py"

    def test_guardrails_prompts_reuse_shared_constants(self):
        src = _src(self.GUARDRAILS_PROMPTS)
        assert "参考此范围描述来决策" not in src, "中文前缀字面量必须只存在于 lg_prompts"
        assert "参考图表结构来回答" not in src
        assert "GUARDRAILS_SCOPE_PREFIX" in src
        assert "GUARDRAILS_GRAPH_PREFIX" in src
        assert "GUARDRAILS_QUESTION_SUFFIX" in src

    def test_schema_utils_reuse_shared_constants(self):
        src = _src(self.SCHEMA_UTILS)
        assert "参考此范围描述来决策" not in src
        assert "参考图表结构来回答" not in src
        assert "GUARDRAILS_SCOPE_PREFIX" in src
        assert "GUARDRAILS_GRAPH_PREFIX" in src

    def test_shared_constants_compose_byte_identical_live_site(self):
        """活代码站点：常量拼接必须与原内联 f-string 逐字节一致。"""
        from agentix.application.agents.lg_prompts import (
            GUARDRAILS_GRAPH_PREFIX,
            GUARDRAILS_QUESTION_SUFFIX,
            GUARDRAILS_SCOPE_PREFIX,
        )

        assert GUARDRAILS_SCOPE_PREFIX + "范围X" == "参考此范围描述来决策:\n范围X"
        assert GUARDRAILS_GRAPH_PREFIX + "schema-text" == "\n参考图表结构来回答:\nschema-text"
        assert "范围X" + "schema-text" + GUARDRAILS_QUESTION_SUFFIX == (
            "范围X" + "schema-text" + "\nQuestion: {question}"
        )

    def test_shared_constants_compose_byte_identical_schema_utils(self):
        """死代码站点：[1:] 去前导换行 + 尾部 "\\n\\n" 必须与历史 f-string 逐字节一致。"""
        from agentix.application.agents.lg_prompts import (
            GUARDRAILS_GRAPH_PREFIX,
            GUARDRAILS_SCOPE_PREFIX,
        )

        assert f"{GUARDRAILS_SCOPE_PREFIX}范围X\n\n" == "参考此范围描述来决策:\n范围X\n\n"
        assert f"{GUARDRAILS_GRAPH_PREFIX[1:]}schema-text\n\n" == (
            "参考图表结构来回答:\nschema-text\n\n"
        )

    def test_rendered_guardrails_template_unchanged(self):
        """端到端：create_guardrails_prompt_template 渲染出的 human 消息与合并前一致。"""
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.guardrails.prompts import (
            create_guardrails_prompt_template,
        )

        template = create_guardrails_prompt_template(
            graph=None, scope_description="劳动法咨询"
        )
        messages = template.messages
        # system 消息必须仍是外置的 GUARDRAILS_SYSTEM_PROMPT（静态在前）
        assert "范围判定助手" in messages[0].prompt.template
        # human 消息：常量拼接后的渲染结果与合并前的内联拼接逐字节一致
        human = messages[1].prompt.template
        assert human == "参考此范围描述来决策:\n劳动法咨询\nQuestion: {question}"
