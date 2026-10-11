"""retrieval SSE progress 事件接线（接口审查批次 2026-09-27）。

后端此前从不产出 ``type=progress``，前端 ``chatStreamHandlers`` /
``useChatStream`` 的 progress 分支是死代码。钉死：

1. 编排层三个**真实边界**按序产出 progress：loading（准备会话）→
   retrieval（检索预检）→ llm_generate（出答）；
2. 形状含 ``stage/message/detail{step,total}``（前端契约字段）；
3. 主链路（meta → answer_chunk → finished）不受影响；
4. 只打真实边界——sparse/dense/rrf 后端无对应划分，不虚报阶段。
"""

from __future__ import annotations

import pytest


class _FakeMsg:
    id = 42


@pytest.mark.asyncio
async def test_stream_emits_progress_at_real_boundaries(monkeypatch):
    from agentix.retrieval.usecases import chat_service as cs
    from agentix.retrieval.usecases import chat_service_helpers as helpers

    # 替换 helpers 属性前捕获真实前奏函数（下方 B2 双写直测要用）
    real_prelude_fn = helpers._stream_thinking_prelude_if_deep

    monkeypatch.setattr(
        cs,
        "_prepare_conversation_and_scope",
        lambda *a, **kw: ("cid-1", _FakeMsg(), "kb", "kb-1"),
    )
    monkeypatch.setattr(
        cs, "_ensure_kb_ready_or_reject", lambda *a, **kw: (object(), None, None, None)
    )
    monkeypatch.setattr(cs, "count_documents_in_scope", lambda *a, **kw: 3)

    async def fake_prepare_context(*a, **kw):
        return {"history_text": "", "scope_label": ""}

    async def fake_prelude(*a, **kw):
        return
        yield  # pragma: no cover（保持 async generator 形状）

    async def fake_preflight(*a, **kw):
        return None  # None = 继续主链路

    async def fake_final(*a, **kw):
        yield {"type": "answer_chunk", "content": "答"}
        yield {"type": "finished", "finished": True}

    monkeypatch.setattr(helpers, "_prepare_context", fake_prepare_context)
    monkeypatch.setattr(helpers, "_stream_thinking_prelude_if_deep", fake_prelude)
    monkeypatch.setattr(helpers, "_stream_retrieval_preflight", fake_preflight)
    monkeypatch.setattr(helpers, "_stream_final_answer", fake_final)

    events = [
        p
        async for p in cs._stream_answer_events_impl(
            session=object(),
            kb_id="kb-1",
            question="q",
            conversation_id=None,
            request_id="req-1",
            user_id="u-1",
            cache_redis=None,
            model_name="m",
        )
    ]

    progress = [e for e in events if e.get("type") == "progress"]
    assert [p["stage"] for p in progress] == ["loading", "retrieval", "llm_generate"]
    for p in progress:
        assert p["message"], "progress 必须带前端直接渲染的 message 文案"
        assert set(p["detail"]) == {"step", "total"}
    assert [p["detail"]["step"] for p in progress] == [1, 2, 3]
    assert all(p["detail"]["total"] == 3 for p in progress)

    types = [e.get("type") for e in events]
    # meta 最先（前端要尽早拿 conversation_id），主链路完好
    assert types[0] == "meta"
    assert "answer_chunk" in types
    assert "finished" in types

    # A5/R5b（SSE 词汇归一）：真实链路构造的帧以 type 为唯一词汇、不带
    # event（monkeypatch 的 fake 帧不在此列——它们不经真实构造点）。
    for e in events:
        if e.get("type") in ("meta", "progress"):
            assert "event" not in e, f"A5 归一后 event 词必须移除: {e}"

    # R5b 补充：thinking_chunk 前奏的**真实构造点**（helpers 直测，
    # 上方 fake_prelude 已替换 helpers 上的属性引用，此处用替换前捕获
    # 的真实函数）同样只带 type。
    async def fake_prelude_stream(**kw):
        yield "想"

    monkeypatch.setattr(helpers, "stream_thinking_prelude", fake_prelude_stream)
    think_frames = [
        f
        async for f in real_prelude_fn(
            chat_mode="deep",
            model_name="m",
            question="q",
            history_text="",
            scope_label="",
            scope_doc_count=0,
            request_id="req-1",
        )
    ]
    assert think_frames, "deep 模式应产出 thinking_chunk"
    for f in think_frames:
        assert f["type"] == "thinking_chunk"
        assert "event" not in f, f"A5 归一后 thinking_chunk 不得带 event: {f}"


@pytest.mark.asyncio
async def test_progress_skips_when_kb_not_ready(monkeypatch):
    """就绪检查拒答时：已有 loading，不应出现 retrieval/llm_generate。"""
    from agentix.retrieval.usecases import chat_service as cs

    monkeypatch.setattr(
        cs,
        "_prepare_conversation_and_scope",
        lambda *a, **kw: ("cid-1", _FakeMsg(), "kb", "kb-1"),
    )
    monkeypatch.setattr(
        cs,
        "_ensure_kb_ready_or_reject",
        lambda *a, **kw: {"type": "error", "content": "知识库为空"},
    )

    events = [
        p
        async for p in cs._stream_answer_events_impl(
            session=object(),
            kb_id="kb-1",
            question="q",
            conversation_id=None,
            request_id="req-1",
            user_id="u-1",
            cache_redis=None,
            model_name="m",
        )
    ]

    stages = [e["stage"] for e in events if e.get("type") == "progress"]
    assert stages == ["loading"]
    assert any(e.get("type") == "error" for e in events)
