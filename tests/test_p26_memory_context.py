"""批次 7：长期记忆读回接线验收测试（全离线，无 Redis / LLM / Docker）。

设计要点（见方案 §10.9.8）：
- 记忆上下文经 ``config["configurable"]["memory_context"]`` 传递；
- general 节点把它作为**瞬态** system 消息注入本次 ainvoke——不写回
  ``state.messages``，因此不会被 ``add_messages`` 持久化、不污染历史；
- ``MEMORY_CONTEXT_ENABLED`` 默认 **False**：零开销、零行为变化。
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agentix.application.agents import general_handler
from agentix.application.agents.general_handler import respond_to_general_query
from agentix.orchestration.langgraph.lg_states import AgentState
from agentix.config import settings as app_settings


class _CapturingModel:
    def __init__(self):
        self.calls = []

    async def ainvoke(self, messages, config=None, **kwargs):
        self.calls.append((messages, config))
        return AIMessage(content="好的，已了解。")


def _state():
    return AgentState(messages=[HumanMessage(content="你好呀")])


class TestGeneralNodeMemoryInjection:
    @pytest.mark.asyncio
    async def test_memory_context_injected_as_transient_system(self, monkeypatch):
        model = _CapturingModel()
        monkeypatch.setattr(general_handler, "get_llm_client", lambda **kw: model)

        config = {"configurable": {"memory_context": "用户历史：共 3 次会话，关注劳动法"}}
        result = await respond_to_general_query(_state(), config=config)

        messages = model.calls[0][0]
        assert len(messages) == 3
        # 主 system（静态在前）→ 记忆 system（瞬态）→ human
        assert "general" in messages[0]["content"] or "助手" in messages[0]["content"]
        assert "历史记忆摘要" in messages[1]["content"]
        assert "关注劳动法" in messages[1]["content"]
        assert messages[2].content == "你好呀"
        # 返回更新只含 AI 回复；原 state.messages 未被写入记忆内容
        assert [m.content for m in result["messages"]] == ["好的，已了解。"]
        assert len(result["messages"]) == 1

    @pytest.mark.asyncio
    async def test_without_memory_context_behaviour_unchanged(self, monkeypatch):
        model = _CapturingModel()
        monkeypatch.setattr(general_handler, "get_llm_client", lambda **kw: model)

        await respond_to_general_query(_state(), config={"configurable": {}})
        messages = model.calls[0][0]
        # 与旧行为逐位一致：system + human，无记忆消息
        assert len(messages) == 2
        assert messages[1].content == "你好呀"

    @pytest.mark.asyncio
    async def test_empty_memory_context_not_injected(self, monkeypatch):
        model = _CapturingModel()
        monkeypatch.setattr(general_handler, "get_llm_client", lambda **kw: model)

        await respond_to_general_query(
            _state(), config={"configurable": {"memory_context": ""}}
        )
        assert len(model.calls[0][0]) == 2


class TestMaybeMemoryContext:
    def _patch_enabled(self, monkeypatch, enabled):
        monkeypatch.setattr(app_settings, "MEMORY_CONTEXT_ENABLED", enabled)
        monkeypatch.setattr(app_settings, "MEMORY_RECALL_LIMIT", 3)

    @pytest.mark.asyncio
    async def test_disabled_returns_empty_without_calling_ltm(self, monkeypatch):
        import asyncio

        from agentix.application.services import chat_service
        from agentix.application.services import long_term_memory as ltm

        def _boom():
            raise AssertionError("关闭时不得触达长期记忆服务")

        monkeypatch.setattr(ltm, "get_long_term_memory", _boom)
        self._patch_enabled(monkeypatch, False)
        result = await chat_service._maybe_memory_context("咨询劳动合同")
        assert result == ""
        del asyncio  # 保持导入对称（无实际用途）

    @pytest.mark.asyncio
    async def test_enabled_returns_context(self, monkeypatch):
        import asyncio

        from agentix.application.services import chat_service
        from agentix.application.services import long_term_memory as ltm

        captured = {}

        class _FakeLTM:
            async def get_context_for_query(self, query, *, max_summaries=3):
                captured["query"] = query
                captured["max_summaries"] = max_summaries
                return "相关历史对话摘要：\n  1. 此前咨询过劳动合同违约"

        monkeypatch.setattr(ltm, "get_long_term_memory", lambda: _FakeLTM())
        self._patch_enabled(monkeypatch, True)
        result = await chat_service._maybe_memory_context("咨询劳动合同")
        assert "劳动合同违约" in result
        assert captured["query"] == "咨询劳动合同"
        assert captured["max_summaries"] == 3
        del asyncio

    @pytest.mark.asyncio
    async def test_ltm_failure_is_non_fatal(self, monkeypatch):
        import asyncio

        from agentix.application.services import chat_service
        from agentix.application.services import long_term_memory as ltm

        def _broken():
            raise RuntimeError("redis down")

        monkeypatch.setattr(ltm, "get_long_term_memory", _broken)
        self._patch_enabled(monkeypatch, True)
        result = await chat_service._maybe_memory_context("咨询劳动合同")
        assert result == ""
        del asyncio
