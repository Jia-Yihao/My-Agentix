"""Phase 4 — Memory：scope 隔离、读取时冲突裁决、PII 脱敏。"""

from __future__ import annotations

import pytest

from agentix.agent.memory import InMemoryMemory, redact_pii, resolve_conflicts
from agentix.domain.agent.types import MemoryItem, MemoryKind, MemoryScope


@pytest.mark.asyncio
async def test_scope_isolation():
    mem = InMemoryMemory()
    await mem.remember(
        MemoryItem(content="user likes tea", scope=MemoryScope.USER, subject="u1", kind=MemoryKind.USER)
    )
    await mem.remember(
        MemoryItem(content="task finished", scope=MemoryScope.TASK, subject="t1", kind=MemoryKind.EPISODIC)
    )
    user_hits = await mem.recall(scope="user", subject="u1")
    task_hits = await mem.recall(scope="task", subject="t1")
    assert len(user_hits) == 1 and user_hits[0].content == "user likes tea"
    assert len(task_hits) == 1
    # 跨 subject 不可见
    other = await mem.recall(scope="user", subject="u2")
    assert other == []


@pytest.mark.asyncio
async def test_read_time_conflict_resolution():
    mem = InMemoryMemory()
    await mem.remember(
        MemoryItem(
            content="preference: dark",
            scope=MemoryScope.USER,
            subject="u1",
            kind=MemoryKind.USER,
            tags=("preference",),
            confidence=0.4,
        )
    )
    await mem.remember(
        MemoryItem(
            content="preference: light",
            scope=MemoryScope.USER,
            subject="u1",
            kind=MemoryKind.USER,
            tags=("preference",),
            confidence=0.9,
        )
    )
    hits = await mem.recall(scope="user", subject="u1", query="preference")
    # 读取时裁决：同 key 只留最高 confidence
    assert len(hits) == 1
    assert hits[0].content == "preference: light"


def test_pii_redaction():
    raw = "联系 13812345678 或 a@b.com，身份证 110101199001011234"
    out = redact_pii(raw)
    assert "13812345678" not in out
    assert "a@b.com" not in out
    assert "110101199001011234" not in out
    assert "[PHONE]" in out and "[EMAIL]" in out and "[ID]" in out


@pytest.mark.asyncio
async def test_remember_redacts_before_store():
    mem = InMemoryMemory()
    await mem.remember(
        MemoryItem(
            content="call 13900001111",
            scope=MemoryScope.USER,
            subject="u1",
            kind=MemoryKind.USER,
        )
    )
    stored = mem.all_items()[0]
    assert "13900001111" not in stored.content
    assert stored.meta.get("redacted") is True


def test_resolve_conflicts_keeps_highest():
    items = [
        MemoryItem(content="a", kind=MemoryKind.SEMANTIC, subject="s", confidence=0.2, tags=("k",)),
        MemoryItem(content="b", kind=MemoryKind.SEMANTIC, subject="s", confidence=0.8, tags=("k",)),
    ]
    out = resolve_conflicts(items)
    assert [i.content for i in out] == ["b"]
