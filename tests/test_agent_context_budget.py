"""Phase 5 — Context：稳定前缀、状态栏白名单、预算截断、大块隔离。"""

from __future__ import annotations

import pytest

from agentix.agent.context import STATUS_BAR_TRUSTED_KEYS, ContextEngine
from agentix.domain.agent.types import Observation, TaskSpec


@pytest.mark.asyncio
async def test_static_prefix_deterministic_regardless_of_key_order():
    e1 = ContextEngine(status_bar={"route": "chat", "tenant_id": "t1"})
    e2 = ContextEngine(status_bar={"tenant_id": "t1", "route": "chat"})
    s1 = await e1.build(TaskSpec(goal="g", task_id="x"))
    s2 = await e2.build(TaskSpec(goal="g", task_id="x"))
    # 前缀行（STATUS …）顺序一致
    p1 = [ln for ln in s1.prompt.splitlines() if ln.startswith("STATUS ")][0]
    p2 = [ln for ln in s2.prompt.splitlines() if ln.startswith("STATUS ")][0]
    assert p1 == p2
    assert s1.static_prefix == s2.static_prefix


@pytest.mark.asyncio
async def test_status_bar_rejects_untrusted_keys():
    e = ContextEngine(status_bar={"route": "chat", "model_said_status": "hacked"})
    snap = await e.build(TaskSpec(goal="g", task_id="x"))
    assert "route" in snap.status_bar
    assert "model_said_status" not in snap.status_bar
    assert "model_said_status" not in snap.prompt


def test_trusted_keys_documented():
    assert "route" in STATUS_BAR_TRUSTED_KEYS
    assert "tenant_id" in STATUS_BAR_TRUSTED_KEYS


@pytest.mark.asyncio
async def test_budget_truncates_from_trajectory_keeps_prefix():
    e = ContextEngine(status_bar={"route": "chat"}, system_rules=("no invent",))
    traj = [
        Observation(step_id=f"s{i}", content=("word " * 50), ok=True) for i in range(10)
    ]
    snap = await e.build(
        TaskSpec(goal="goal-line", task_id="y"),
        trajectory=traj,
        budget_tokens=40,
    )
    assert snap.truncated is True
    assert "STATUS route=chat" in snap.prompt
    assert "RULE no invent" in snap.prompt
    assert "GOAL goal-line" in snap.prompt
    # 预算后仍保留静态前缀在最前
    assert snap.prompt.index("STATUS route=chat") < snap.prompt.index("GOAL goal-line")


@pytest.mark.asyncio
async def test_large_observation_isolated_not_fully_inlined():
    e = ContextEngine(isolate_over_chars=100)
    big = "X" * 500
    snap = await e.build(
        TaskSpec(goal="g", task_id="z"),
        trajectory=[Observation(step_id="s1", content=big, ok=True)],
    )
    assert snap.meta.get("isolated_refs") == ["obs://s1"]
    assert big not in snap.prompt
    assert "[isolated obs://s1]" in snap.prompt
