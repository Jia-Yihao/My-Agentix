"""批次 7：主图历史消息窗口化验收测试（全离线，无 LLM / Redis / Docker）。

背景：``AgentState.messages`` 走 ``add_messages`` reducer，会话经 checkpointer
跨请求持久化，旧消息无界累积。修复：``lg_builder.build_graph`` 在入口插入
``message_window`` 节点，按 ``settings.MAX_HISTORY_MESSAGES`` 裁剪（``<=0`` 关闭）。

实测依据（本文件即证据）：
- ``RemoveMessage`` 删除语义在 langgraph 0.2.60 + langchain-core 0.3.63 下可用，
  但仅对「已经过 add_messages 分配 id」的消息生效；
- 裁剪后的状态由 checkpointer 持久化，下一轮不再携带被裁剪的旧消息。
"""

import asyncio

import pytest
from langchain_core.messages import HumanMessage, RemoveMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph

from agentix.orchestration.langgraph.lg_builder import create_message_window_node
from agentix.config import settings as app_settings


def _build_state(messages):
    """构造 AgentState（dataclass）实例——与真实图节点收到的形态一致。"""
    from agentix.orchestration.langgraph.lg_states import AgentState

    return AgentState(messages=messages)


class TestMessageWindowNode:
    @pytest.mark.asyncio
    async def test_within_limit_is_noop(self, monkeypatch):
        monkeypatch.setattr(app_settings, "MAX_HISTORY_MESSAGES", 5)
        node = create_message_window_node(5)
        msgs = [HumanMessage(content=f"m{i}") for i in range(3)]
        # 经过一次 add_messages 使 id 落定（真实图状态即如此）
        from langgraph.graph.message import add_messages

        msgs = add_messages(msgs, [])
        result = await node(_build_state(msgs))
        assert result == {"messages": []}

    @pytest.mark.asyncio
    async def test_overflow_trims_to_last_n(self, monkeypatch):
        monkeypatch.setattr(app_settings, "MAX_HISTORY_MESSAGES", 3)
        node = create_message_window_node(3)
        from langgraph.graph.message import add_messages

        msgs = add_messages(
            [HumanMessage(content=f"m{i}") for i in range(6)], []
        )
        result = await node(_build_state(msgs))
        updates = result["messages"]
        assert len(updates) == 3
        assert all(isinstance(u, RemoveMessage) for u in updates)
        # 被删的应是最旧的 3 条
        assert {u.id for u in updates} == {m.id for m in msgs[:3]}

    @pytest.mark.asyncio
    async def test_disabled_is_noop(self, monkeypatch):
        monkeypatch.setattr(app_settings, "MAX_HISTORY_MESSAGES", 0)
        node = create_message_window_node(0)
        from langgraph.graph.message import add_messages

        msgs = add_messages([HumanMessage(content=f"m{i}") for i in range(9)], [])
        result = await node(_build_state(msgs))
        assert result == {"messages": []}

    @pytest.mark.asyncio
    async def test_messages_without_id_are_skipped(self, monkeypatch):
        """无 id 的消息无法被 RemoveMessage 删除——必须跳过而非抛错。"""
        monkeypatch.setattr(app_settings, "MAX_HISTORY_MESSAGES", 1)
        node = create_message_window_node(1)
        bare = [HumanMessage(content="no-id-1"), HumanMessage(content="no-id-2")]
        assert all(m.id is None for m in bare)
        result = await node(_build_state(list(bare)))
        assert result == {"messages": []}


class TestWindowedGraphIntegration:
    """端到端：窗口节点接进 StateGraph + MemorySaver，多轮后长度封顶。"""

    def _build_mini_graph(self, max_messages: int):
        """复用真实工厂 + 真实 AgentState/InputState，但用透传节点替代 LLM 节点。"""
        from agentix.orchestration.langgraph.lg_states import AgentState, InputState

        builder = StateGraph(AgentState, input=InputState)
        builder.add_node("message_window", create_message_window_node(max_messages))
        builder.add_node("_passthrough", _passthrough)
        builder.add_edge(START, "message_window")
        builder.add_edge("message_window", "_passthrough")
        return builder.compile(checkpointer=MemorySaver())

    def test_bounded_growth_across_turns(self):
        graph = self._build_mini_graph(max_messages=3)
        config = {"configurable": {"thread_id": "t-window-1"}}
        for i in range(6):
            asyncio.run(
                graph.ainvoke(
                    {"messages": [{"type": "human", "content": f"turn-{i}"}]},
                    config=config,
                )
            )
        snap = graph.get_state(config)
        assert len(snap.values["messages"]) == 3
        contents = [m.content for m in snap.values["messages"]]
        assert contents == ["turn-3", "turn-4", "turn-5"]

    def test_disabled_grows_unbounded(self, monkeypatch):
        monkeypatch.setattr(app_settings, "MAX_HISTORY_MESSAGES", 0)
        graph = self._build_mini_graph(max_messages=0)
        config = {"configurable": {"thread_id": "t-window-0"}}
        for i in range(4):
            asyncio.run(
                graph.ainvoke(
                    {"messages": [{"type": "human", "content": f"turn-{i}"}]},
                    config=config,
                )
            )
        snap = graph.get_state(config)
        assert len(snap.values["messages"]) == 4

    def test_sessions_are_isolated(self):
        graph = self._build_mini_graph(max_messages=2)
        for tid in ("t-iso-a", "t-iso-b"):
            for i in range(3):
                asyncio.run(
                    graph.ainvoke(
                        {"messages": [{"type": "human", "content": f"{tid}-{i}"}]},
                        config={"configurable": {"thread_id": tid}},
                    )
                )
        for tid in ("t-iso-a", "t-iso-b"):
            snap = graph.get_state({"configurable": {"thread_id": tid}})
            assert [m.content for m in snap.values["messages"]] == [f"{tid}-1", f"{tid}-2"]


async def _passthrough(state) -> dict:
    """不做任何事（空更新），仅用于承载最小图拓扑。"""
    return {"messages": []}
