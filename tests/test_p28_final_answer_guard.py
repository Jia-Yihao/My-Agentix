"""批次 10（§10.10.1）——KG 子图终态验证器 final_answer_guard 守卫测试。

验证四分支 + 拓扑接线 + 默认关：
1. ``KG_FINAL_ANSWER_VALIDATION=False``（默认）→ 节点 no-op，**零 LLM 调用**；
2. valid=True → steps 记 ``final_answer_validated``；
3. valid=False → 降级放行（不扣答案），steps 记 ``final_answer_validation_failed``；
4. 校验链异常 → 降级放行，steps 记 ``final_answer_validation_error``。

运行：./.venv/Scripts/python.exe -m pytest tests/test_p28_final_answer_guard.py -q
"""
from unittest.mock import MagicMock

import pytest
from langchain_core.runnables.base import RunnableLambda

from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.final_answer_guard import (
    create_final_answer_guard,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.validate_final_answer.models import (
    ValidateFinalAnswerResponse,
)
from agentix.config import settings as settings_instance


class FakeGuardLLM:
    """``with_structured_output`` 返回固定响应/固定异常的假 LLM。

    返回 RunnableLambda（async 函数）以融入 ``prompt | structured`` 链。
    ``calls`` 记录真实发生过的校验调用次数（用于证明关闭分支零调用）。
    """

    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = 0

    def with_structured_output(self, schema):
        async def _respond(_inputs):
            self.calls += 1
            if self.exc is not None:
                raise self.exc
            return self.response

        return RunnableLambda(_respond)


@pytest.fixture()
def guard_graph():
    return MagicMock()


@pytest.fixture()
def force_enabled(monkeypatch):
    monkeypatch.setattr(settings_instance, "KG_FINAL_ANSWER_VALIDATION", True)


class TestDefaultDisabled:
    def test_default_setting_is_off(self):
        """默认配置必须是关（零行为变化的前提）。"""
        assert settings_instance.KG_FINAL_ANSWER_VALIDATION is False

    @pytest.mark.asyncio
    async def test_disabled_guard_is_noop_and_calls_no_llm(self, guard_graph, monkeypatch):
        """关闭时：no-op（steps 零增量）且**零 LLM 调用**（零成本声明）。"""
        monkeypatch.setattr(settings_instance, "KG_FINAL_ANSWER_VALIDATION", False)
        # 若 guard 真的调了 LLM，fake 会抛 AssertionError 使测试失败
        llm = FakeGuardLLM(response=ValidateFinalAnswerResponse(valid=False))
        guard = create_final_answer_guard(llm=llm, graph=guard_graph)

        result = await guard({"question": "q", "summary": "s"})

        assert result == {}
        assert llm.calls == 0


class TestEnabledBranches:
    @pytest.mark.asyncio
    async def test_valid_answer_records_validated_step(self, guard_graph, force_enabled):
        llm = FakeGuardLLM(response=ValidateFinalAnswerResponse(valid=True))
        guard = create_final_answer_guard(llm=llm, graph=guard_graph)

        result = await guard({"question": "q", "summary": "s"})

        assert result == {"steps": ["final_answer_validated"]}
        assert llm.calls == 1

    @pytest.mark.asyncio
    async def test_invalid_answer_degrade_pass_with_trace(
        self, guard_graph, force_enabled
    ):
        """校验失败 → 降级放行：只留痕 + warning，绝不返回「扣答案」类增量。"""
        llm = FakeGuardLLM(
            response=ValidateFinalAnswerResponse(
                valid=False, follow_up_question="还需要哪些条款细节？"
            )
        )
        guard = create_final_answer_guard(llm=llm, graph=guard_graph)

        result = await guard({"question": "q", "summary": "s"})

        assert result == {"steps": ["final_answer_validation_failed"]}
        assert llm.calls == 1

    @pytest.mark.asyncio
    async def test_validation_error_degrade_pass_with_trace(
        self, guard_graph, force_enabled
    ):
        """校验链异常 → 降级放行：异常必须被吞掉，不冒泡阻断主链路。"""
        llm = FakeGuardLLM(exc=RuntimeError("LLM upstream 500"))
        guard = create_final_answer_guard(llm=llm, graph=guard_graph)

        result = await guard({"question": "q", "summary": "s"})

        assert result == {"steps": ["final_answer_validation_error"]}
        assert llm.calls == 1

    @pytest.mark.asyncio
    async def test_guard_never_touches_answer_or_summary(
        self, guard_graph, force_enabled
    ):
        """降级放行的语义边界：guard 增量里不得出现 summary/answer 等字段。"""
        llm = FakeGuardLLM(response=ValidateFinalAnswerResponse(valid=False))
        guard = create_final_answer_guard(llm=llm, graph=guard_graph)

        result = await guard({"question": "q", "summary": "s"})

        assert set(result.keys()) == {"steps"}


class TestTopology:
    @pytest.fixture(scope="class")
    def compiled(self):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.multi_tool import (
            create_multi_tool_workflow,
        )

        return create_multi_tool_workflow(
            llm=MagicMock(),
            graph=MagicMock(),
            tool_schemas=[],
            predefined_cypher_dict={},
            cypher_example_retriever=MagicMock(),
        )

    def test_guard_node_registered(self, compiled):
        assert "final_answer_guard" in set(compiled.get_graph().nodes.keys())

    def test_summarize_goes_through_guard_to_final_answer(self, compiled):
        edges = {(e.source, e.target) for e in compiled.get_graph().edges}
        assert ("summarize", "final_answer_guard") in edges
        assert ("final_answer_guard", "final_answer") in edges
        # 旧直连边必须已拆除
        assert ("summarize", "final_answer") not in edges

    def test_guard_node_not_in_stream_forward_whitelist(self):
        """硬性契约 1 双保险：guard 内部编排调用不得被当答案流转发。"""
        from agentix.application.services.chat_service import ChatService

        assert "final_answer_guard" not in ChatService._STREAM_FORWARD_NODES
