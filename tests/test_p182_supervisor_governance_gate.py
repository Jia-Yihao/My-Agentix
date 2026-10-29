"""p182：supervisor 三处治理闸（M3-4b 卡1，只加闸不拆边）。

验收口径：

1. ``lg_supervisor.supervisor_dispatch`` 内三处宽 ``except Exception``
   （proposer 修订轮 / reviewer fail-open / 外层 ``manager.run``）之前各有一条
   ``except capabilities.CapabilityError: raise``——注入位装配缺失（如
   ``CapabilityNotRegistered``）必须 fail-closed 抛出，不许被伪装成
   「审阅通过 approve」或「回退草稿（manager_exception）」。这是本机制最坏的
   失效形态：装配没跑却交付全绿。
2. 闸要窄：只挡 ``CapabilityError``。普通 LLM 故障（``RuntimeError`` 等）仍
   fail-open——由既有用例 ``tests/test_agent_phase19_multiagent.py`` 的
   ``test_llm_review_failure_fails_open_to_draft`` 与
   ``test_manager_exception_falls_back_to_draft`` 反向钉住，本文件不改也不重复它们。

形态复用：子 agent 替身 ``_CountingHandler`` 直接 import phase19 的同名实现
（纪律 6 ④：同一个形态不写两份）；``_state`` 按该文件 ``:40-43`` 的构造同形重建。
判据只认返回值/异常，不认日志。
"""

import pytest
from langchain_core.messages import HumanMessage

from agentix.orchestration import capabilities
from agentix.orchestration.langgraph import lg_supervisor
from agentix.orchestration.langgraph.lg_states import AgentState, Router
from tests.test_agent_phase19_multiagent import _CountingHandler


def _state(question: str = "违约金怎么算？", route_type: str = "general-query") -> AgentState:
    state = AgentState(messages=[HumanMessage(content=question)])
    state.router = Router(type=route_type, logic="test")
    return state


def _not_registered() -> capabilities.CapabilityNotRegistered:
    """构造一个「注入位没装配」的现场（与 capabilities.py 的抛出点同形）。"""
    return capabilities.CapabilityNotRegistered(["get_llm_client"])


@pytest.mark.asyncio
async def test_reviewer_capability_error_not_swallowed(monkeypatch):
    """闸②（reviewer）：CapabilityError 不许被 fail-open 吞成 approve。

    改前现状：宽 except 吞掉后返回 review_text="approve"、meta["ok"] is True——
    「审阅通过」与「审阅根本没跑起来」被伪装成同一件事，故本用例先红。
    """
    handler = _CountingHandler()
    monkeypatch.setitem(lg_supervisor.ROUTE_HANDLERS, "respond_to_general_query", handler)
    monkeypatch.setattr(lg_supervisor, "_llm_available", lambda: True)

    async def boom(*, goal: str, proposal: str) -> str:
        raise _not_registered()

    monkeypatch.setattr(lg_supervisor, "_llm_review", boom)

    with pytest.raises(capabilities.CapabilityError):
        await lg_supervisor.supervisor_dispatch(_state(), config={})


@pytest.mark.asyncio
async def test_proposer_revise_capability_error_not_swallowed(monkeypatch):
    """闸①（proposer 修订轮）：round>=2 的 _llm_revise 抛 CapabilityError 必须出栈。

    路径取 phase19 ``:200-233`` 的同一条：先让审阅判 reject 进第 2 轮，修订轮
    才可达。改前现状：宽 except 吞掉后 revised=""、退回草稿继续走，全链无异常，
    故本用例先红。
    """
    handler = _CountingHandler()
    monkeypatch.setitem(lg_supervisor.ROUTE_HANDLERS, "respond_to_general_query", handler)
    monkeypatch.setattr(lg_supervisor, "_llm_available", lambda: True)

    async def reject_review(*, goal: str, proposal: str) -> str:
        return "reject: 引用不完整"

    async def boom_revise(*, goal: str, proposal: str, feedback: str) -> str:
        raise _not_registered()

    monkeypatch.setattr(lg_supervisor, "_llm_review", reject_review)
    monkeypatch.setattr(lg_supervisor, "_llm_revise", boom_revise)

    with pytest.raises(capabilities.CapabilityError):
        await lg_supervisor.supervisor_dispatch(_state(), config={})


@pytest.mark.asyncio
async def test_outer_manager_capability_error_not_swallowed(monkeypatch):
    """闸③（外层 manager.run）：独立于内层回调，直接验证外层挡位。

    Manager 整体抛 CapabilityError（等价于内层 re-raise 出栈后的形态）。
    改前现状：走 ``:183-198`` 已登记的形态——返回草稿且
    ``meta["failure_mode"] == "manager_exception"``，无异常出栈，故本用例先红。
    """
    handler = _CountingHandler()
    monkeypatch.setitem(lg_supervisor.ROUTE_HANDLERS, "respond_to_general_query", handler)

    class _BoomManager:
        def __init__(self, **kwargs):
            pass

        async def run(self, goal, **kwargs):
            raise _not_registered()

    monkeypatch.setattr(lg_supervisor, "Manager", _BoomManager)

    with pytest.raises(capabilities.CapabilityError):
        await lg_supervisor.supervisor_dispatch(_state(), config={})
