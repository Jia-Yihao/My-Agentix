"""Phase 24（Ch3 记忆/知识深化）落地测试。

覆盖面（对齐 docs/migration/Phase21-26_规划.md Phase 24 DoD）：

1. 遗忘三动作生产化：
   - 摘要淘汰**先归档正文再删原键**（证据可回溯，禁止静默销毁）；
   - `memory_consolidation` task handler：flag-off 零存储触碰、
     幂等时间窗（SET NX EX）、dry_run 不占窗、force/窗口关闭旁路。
2. 重要性三因素纯函数：访问频率 / 时间衰减 / 独特性 + 淘汰排序接线。
3. Agentic RAG（`AGENTIC_RAG_ENABLED` 默认关）：
   - flag-off 单次 workflow invoke、零改写、零轨迹（与基线等价）；
   - flag-on 空检索 → 改写重试 ≤2（总步数有界）→ 仍空保留诚实文案；
   - 每次尝试进 trajectory（kind=retrieval, name=agentic_rag_rewrite），
     观测失败不影响主链路；改写 LLM 失败降级不崩。
4. 读取路径只更新访问统计，**不做淘汰/归档裁决**（读时裁决不回写）。
5. 改写 prompt 外置（p23 三断言同款）。

全程离线：FakeRedis + 假 workflow + 假 LLM，零外部依赖。
"""

import json
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agentix.application.agents import general_handler
from agentix.orchestration.langgraph.lg_states import AgentState
from agentix.config import settings as app_settings
from agentix.application.services import long_term_memory as ltm_module
from agentix.application.services.long_term_memory import LongTermMemory, MemorySummary
from agentix.application.services.task_handlers import handle_memory_consolidation
from fakes.redis import FakeRedis  # type: ignore[import-not-found]

DAY = 86400.0


def _state(question: str = "违约金怎么算？") -> AgentState:
    return AgentState(messages=[HumanMessage(content=question)])


def _summary(
    sid: str,
    *,
    topics,
    created_at: float,
    access: int = 0,
    last: float = None,  # type: ignore[assignment]
    sources=(),
) -> MemorySummary:
    return MemorySummary(
        session_id=sid,
        user_id="u1",
        summary=f"摘要-{sid}",
        topics=list(topics),
        key_facts=[],
        message_count=1,
        created_at=created_at,
        access_count=access,
        last_accessed=created_at if last is None else last,
        source_message_ids=list(sources),
    )


async def _seed(mem, session_id, *, topics, age_days, sources, access=0, summary="摘要"):
    now = time.time()
    created = now - age_days * DAY
    return await mem._redis.set(
        f"ltm:summary:{session_id}",
        json.dumps(
            {
                "session_id": session_id,
                "user_id": "u1",
                "summary": summary,
                "topics": list(topics),
                "key_facts": [],
                "message_count": 1,
                "created_at": created,
                "access_count": access,
                "last_accessed": created,
                "source_message_ids": list(sources),
            }
        ),
    )


def _memory() -> LongTermMemory:
    return LongTermMemory(redis_client=FakeRedis())


# ---------------------------------------------------------------------------
# 1. 遗忘生产化：淘汰归档（证据可回溯红线）
# ---------------------------------------------------------------------------


class TestEvictionArchival:
    @pytest.mark.asyncio
    async def test_evicted_summary_is_archived_with_evidence(self):
        """淘汰的摘要先归档正文（含 source_message_ids），再删原键。"""
        mem = _memory()
        await _seed(mem, "old", topics=["租赁"], age_days=400, sources=["m0"])
        await mem._redis.zadd("ltm:user:u1:sessions", {"old": time.time() - 400 * DAY})

        report = await mem.consolidate("u1", ttl_days=90)

        assert report["evicted"] == 1
        assert report["archived_session_ids"] == ["old"]
        archived_raw = await mem._redis.get("ltm:archived:summary:old")
        assert archived_raw is not None, "淘汰红线：正文必须归档，禁止静默销毁"
        archived = json.loads(archived_raw)
        assert archived["source_message_ids"] == ["m0"], "证据链必须随归档保留"
        assert await mem._redis.get("ltm:summary:old") is None, "原键应已删除"

    @pytest.mark.asyncio
    async def test_dry_run_reports_archived_without_writing(self):
        mem = _memory()
        await _seed(mem, "old", topics=["租赁"], age_days=400, sources=["m0"])
        await mem._redis.zadd("ltm:user:u1:sessions", {"old": time.time() - 400 * DAY})

        report = await mem.consolidate("u1", ttl_days=90, dry_run=True)

        assert report["dry_run"] is True
        assert report["archived_session_ids"] == ["old"], "计划必须可观察"
        assert await mem._redis.get("ltm:archived:summary:old") is None, "dry-run 零写入"
        assert await mem._redis.get("ltm:summary:old") is not None, "dry-run 不删原键"

    @pytest.mark.asyncio
    async def test_merged_away_is_not_archived(self):
        """被合并掉的条目正文已完整并入合并结果（并集语义），不再另存归档。"""
        mem = _memory()
        await _seed(mem, "s1", topics=["租赁"], age_days=5, sources=["m0"])
        await _seed(mem, "s2", topics=["租赁"], age_days=4, sources=["m1"])
        for sid, age in (("s1", 5), ("s2", 4)):
            await mem._redis.zadd("ltm:user:u1:sessions", {sid: time.time() - age * DAY})

        report = await mem.consolidate("u1")

        assert report["merged_away"] == 1
        assert report["archived_session_ids"] == []
        assert await mem._redis.get("ltm:archived:summary:s2") is None
        merged = json.loads(await mem._redis.get("ltm:summary:s1"))
        assert merged["source_message_ids"] == ["m0", "m1"], "并集溯源保留"

    @pytest.mark.asyncio
    async def test_empty_user_report_shape_is_stable(self):
        mem = _memory()
        report = await mem.consolidate("nobody")
        assert report["archived_session_ids"] == []
        assert report["merged_session_ids"] == []
        assert report["evicted_session_ids"] == []


# ---------------------------------------------------------------------------
# 2. 读取路径：只更新访问统计，不做淘汰/归档裁决
# ---------------------------------------------------------------------------


class TestReadPathDoesNotEvict:
    @pytest.mark.asyncio
    async def test_context_query_never_evicts_or_archives(self):
        mem = _memory()
        await _seed(mem, "s1", topics=["租赁"], age_days=400, sources=["m0"])
        await _seed(mem, "s2", topics=["劳动"], age_days=1, sources=["m9"])
        for sid in ("s1", "s2"):
            await mem._redis.zadd("ltm:user:u1:sessions", {sid: time.time() - DAY})
        keys_before = set(mem._redis.store)

        await mem.get_context_for_query("租赁", "u1")

        keys_after = set(mem._redis.store)
        assert keys_after == keys_before, "读路径不得增删键（无归档、无淘汰）"
        assert not any(":archived:" in k for k in keys_after)
        # 访问统计回写是读路径的既有行为（access_count/last_accessed），允许但不改键集
        touched = json.loads(await mem._redis.get("ltm:summary:s1"))
        assert touched["access_count"] == 1


# ---------------------------------------------------------------------------
# 3. 重要性三因素纯函数（访问频率 / 时间衰减 / 独特性）
# ---------------------------------------------------------------------------


class TestImportanceFactors:
    def test_access_frequency_score(self):
        from agentix.application.services.memory_consolidation import access_frequency_score

        now = time.time()
        assert access_frequency_score(_summary("a", topics=["x"], created_at=now, access=5)) == 5.0
        assert access_frequency_score(_summary("b", topics=["x"], created_at=now, access=-3)) == 0.0

    def test_recency_decay_half_life(self):
        from agentix.application.services.memory_consolidation import recency_decay_score

        now = time.time()
        fresh = _summary("a", topics=["x"], created_at=now - 30 * DAY, last=now - 30 * DAY)
        assert recency_decay_score(fresh, now=now, half_life_days=30.0) == pytest.approx(0.5, abs=1e-9)
        newer = _summary("b", topics=["x"], created_at=now - DAY, last=now - DAY)
        older = _summary("c", topics=["x"], created_at=now - 90 * DAY, last=now - 90 * DAY)
        assert recency_decay_score(newer, now=now) > recency_decay_score(older, now=now)

    def test_recency_decay_edge_cases(self):
        from agentix.application.services.memory_consolidation import recency_decay_score

        now = time.time()
        never = _summary("a", topics=["x"], created_at=now, last=0.0)
        assert recency_decay_score(never, now=now) == 0.0, "从未访问 ⇒ 0.0"
        active = _summary("b", topics=["x"], created_at=now - 100 * DAY, last=now - DAY)
        assert recency_decay_score(active, now=now, half_life_days=0.0) == 1.0, "半衰期关闭 ⇒ 不衰减"
        future = _summary("c", topics=["x"], created_at=now, last=now + DAY)
        assert recency_decay_score(future, now=now) == 1.0, "时钟回拨夹紧到 1.0"

    def test_uniqueness_score(self):
        from agentix.application.services.memory_consolidation import uniqueness_score

        counts = {"租赁": 5, "押花": 1}
        now = time.time()
        assert uniqueness_score(_summary("a", topics=["租赁"], created_at=now), counts) == 0.0
        assert uniqueness_score(_summary("b", topics=["押花"], created_at=now), counts) == 1.0
        assert uniqueness_score(_summary("c", topics=["租赁", "押花"], created_at=now), counts) == 0.5
        assert uniqueness_score(_summary("d", topics=[], created_at=now), counts) == 0.0

    def test_importance_sort_key_tie_break_ordering(self):
        """字典序：(访问频率, 时间衰减, 独特性) 升序——频率高者后淘汰，
        同频率更清新者后淘汰，其余相同则主题更稀有者后淘汰。"""
        from agentix.application.services.memory_consolidation import importance_sort_key

        now = time.time()
        common = _summary("a", topics=["租赁"], created_at=now - DAY, access=2, last=now - DAY)
        rare = _summary("b", topics=["押花"], created_at=now - DAY, access=2, last=now - DAY)
        low_access = _summary("c", topics=["押花"], created_at=now, access=1, last=now)
        # 同频率下：common(0.0) 先走、rare(1.0) 后走
        assert importance_sort_key(common, now=now, topic_counts={"租赁": 5, "押花": 1}) < (
            importance_sort_key(rare, now=now, topic_counts={"租赁": 5, "押花": 1})
        )
        # 第一键压倒第三键：低频率即便更新也先走
        assert importance_sort_key(low_access, now=now, topic_counts={"押花": 1}) < (
            importance_sort_key(common, now=now, topic_counts={"租赁": 5, "押花": 1})
        )

    def test_capacity_eviction_prefers_unimportant(self):
        from agentix.application.services.memory_consolidation import plan_summary_eviction

        now = time.time()
        # 3 条全部新鲜（不过期），max_items=1 ⇒ 按重要性淘汰 2 条
        hot = _summary("hot", topics=["租赁"], created_at=now - DAY, access=9, last=now - DAY)
        cold_common = _summary("cold_common", topics=["租赁"], created_at=now - DAY, access=0, last=now - DAY)
        cold_rare = _summary("cold_rare", topics=["押花"], created_at=now - DAY, access=0, last=now - DAY)

        keep, evict = plan_summary_eviction(
            [hot, cold_common, cold_rare],
            now=now,
            ttl_days=0,
            max_items=1,
            half_life_days=30.0,
        )

        assert [m.session_id for m in keep] == ["hot"], "高频访问的记忆必须保留"
        assert sorted(m.session_id for m in evict) == ["cold_common", "cold_rare"]
        # 同为零访问：主题更常见的先走（第三键 tie-break）
        assert evict[0].session_id == "cold_common"


# ---------------------------------------------------------------------------
# 4. memory_consolidation handler：flag 闸 + 幂等窗口
# ---------------------------------------------------------------------------


def _install_singleton(monkeypatch) -> LongTermMemory:
    mem = _memory()
    monkeypatch.setattr(ltm_module, "_long_term_memory_instance", mem)
    return mem


class TestConsolidationHandler:
    @pytest.mark.asyncio
    async def test_flag_off_skips_without_touching_storage(self, monkeypatch):
        monkeypatch.setattr(app_settings, "MEMORY_CONSOLIDATION_ENABLED", False)
        mem = _install_singleton(monkeypatch)
        before = dict(mem._redis.store)

        result = await handle_memory_consolidation({"user_id": "u1"})

        assert result == {"status": "skipped", "reason": "disabled", "user_id": "u1"}
        assert mem._redis.store == before, "flag-off 零存储触碰"

    @pytest.mark.asyncio
    async def test_flag_on_consolidates_then_window_blocks_repeat(self, monkeypatch):
        monkeypatch.setattr(app_settings, "MEMORY_CONSOLIDATION_ENABLED", True)
        mem = _install_singleton(monkeypatch)

        first = await handle_memory_consolidation({"user_id": "u1"})
        second = await handle_memory_consolidation({"user_id": "u1"})

        assert first["status"] == "consolidated"
        assert second["status"] == "skipped"
        assert second["reason"] == "already_consolidated", "同窗口重复触发必须幂等"
        assert "ltm:consolidation:last:u1" in mem._redis.store

    @pytest.mark.asyncio
    async def test_force_bypasses_window(self, monkeypatch):
        monkeypatch.setattr(app_settings, "MEMORY_CONSOLIDATION_ENABLED", True)
        _install_singleton(monkeypatch)

        await handle_memory_consolidation({"user_id": "u1"})
        forced = await handle_memory_consolidation({"user_id": "u1", "force": True})

        assert forced["status"] == "consolidated"

    @pytest.mark.asyncio
    async def test_dry_run_does_not_claim_window(self, monkeypatch):
        monkeypatch.setattr(app_settings, "MEMORY_CONSOLIDATION_ENABLED", True)
        mem = _install_singleton(monkeypatch)

        first = await handle_memory_consolidation({"user_id": "u1", "dry_run": True})
        second = await handle_memory_consolidation({"user_id": "u1", "dry_run": True})

        assert first["status"] == "consolidated"
        assert second["status"] == "consolidated", "dry-run 观察不占窗"
        assert "ltm:consolidation:last:u1" not in mem._redis.store
        assert first["dry_run"] is True

    @pytest.mark.asyncio
    async def test_window_zero_disables_gate(self, monkeypatch):
        monkeypatch.setattr(app_settings, "MEMORY_CONSOLIDATION_ENABLED", True)
        _install_singleton(monkeypatch)

        first = await handle_memory_consolidation({"user_id": "u1", "window_seconds": 0})
        second = await handle_memory_consolidation({"user_id": "u1", "window_seconds": 0})

        assert first["status"] == "consolidated"
        assert second["status"] == "consolidated", "window<=0 关闭幂等窗"


# ---------------------------------------------------------------------------
# 5. Agentic RAG：flag-off 等价 / flag-on 改写重试
# ---------------------------------------------------------------------------

_HONEST = "暂未找到相关记载"


class _FakeWorkflow:
    """可编排响应序列的假 KB workflow；记录每次 invoke 的 payload。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.invocations = []

    async def ainvoke(self, payload, config=None):
        self.invocations.append(payload)
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


class _RewriteModel:
    """假改写 LLM：记录 prompt，返回固定新查询；可配置抛异常。"""

    def __init__(self, rewritten="租赁合同违约金 计算标准", raise_exc=None):
        self.rewritten = rewritten
        self.raise_exc = raise_exc
        self.ainvoke_calls = []

    async def ainvoke(self, messages, config=None, **kwargs):
        self.ainvoke_calls.append(messages)
        if self.raise_exc is not None:
            raise self.raise_exc
        return AIMessage(content=self.rewritten)


class _StubRecorder:
    def __init__(self):
        self.records = []

    def record(self, *, kind, name, status="ok", detail=None, **kwargs):
        self.records.append(
            {"kind": kind, "name": name, "status": status, "detail": detail or {}}
        )


class _SentinelGateway:
    def __init__(self, *args, **kwargs):
        pass


def _patch_kb_pipeline(monkeypatch, workflow, model, recorder):
    from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent import (
        multi_tool,
    )

    monkeypatch.setattr(multi_tool, "create_kb_multi_tool_workflow", lambda **kw: workflow)
    monkeypatch.setattr(general_handler, "KnowledgeGateway", _SentinelGateway)
    monkeypatch.setattr(general_handler, "get_llm_client", lambda **kw: model)
    monkeypatch.setattr(
        "agentix.application.services.trajectory.get_trajectory_recorder",
        lambda: recorder,
    )


class TestAgenticRagFlagOff:
    @pytest.mark.asyncio
    async def test_single_invoke_zero_rewrite_zero_trajectory(self, monkeypatch):
        monkeypatch.setattr(app_settings, "AGENTIC_RAG_ENABLED", False)
        workflow = _FakeWorkflow([{"answer": _HONEST, "sources": []}])
        model = _RewriteModel()
        recorder = _StubRecorder()
        _patch_kb_pipeline(monkeypatch, workflow, model, recorder)

        result = await general_handler.create_kb_query(_state(), config={})

        assert len(workflow.invocations) == 1, "flag-off 必须单次 invoke（与基线等价）"
        assert model.ainvoke_calls == [], "flag-off 零改写"
        assert recorder.records == [], "flag-off 零轨迹"
        assert result["messages"][0].content == _HONEST

    @pytest.mark.asyncio
    async def test_flag_off_with_sources_returns_unchanged(self, monkeypatch):
        monkeypatch.setattr(app_settings, "AGENTIC_RAG_ENABLED", False)
        workflow = _FakeWorkflow([{"answer": "有据回答", "sources": [{"id": 1}]}])
        model = _RewriteModel()
        recorder = _StubRecorder()
        _patch_kb_pipeline(monkeypatch, workflow, model, recorder)

        result = await general_handler.create_kb_query(_state(), config={})

        assert len(workflow.invocations) == 1
        assert result["messages"][0].content == "有据回答"
        assert result["sources"] == [{"id": 1}]


class TestAgenticRagFlagOn:
    @pytest.mark.asyncio
    async def test_rewrites_until_sources_found(self, monkeypatch):
        monkeypatch.setattr(app_settings, "AGENTIC_RAG_ENABLED", True)
        workflow = _FakeWorkflow(
            [
                {"answer": _HONEST, "sources": []},
                {"answer": "命中回答", "sources": [{"id": 7}]},
            ]
        )
        model = _RewriteModel(rewritten="违约金 计算 标准 法条")
        recorder = _StubRecorder()
        _patch_kb_pipeline(monkeypatch, workflow, model, recorder)

        result = await general_handler.create_kb_query(_state(), config={})

        assert result["messages"][0].content == "命中回答"
        assert len(workflow.invocations) == 2, "一次改写后命中即停"
        assert len(model.ainvoke_calls) == 1
        prompt_text = model.ainvoke_calls[0][0]["content"]
        assert "违约金怎么算" in prompt_text, "改写 prompt 必须携带原查询"
        assert "违约金 计算 标准 法条" in workflow.invocations[1]["question"]
        assert len(recorder.records) == 1
        rec = recorder.records[0]
        assert rec["kind"] == "retrieval"
        assert rec["name"] == "agentic_rag_rewrite"
        assert rec["detail"]["attempt"] == 1
        assert rec["detail"]["hits"] == 1

    @pytest.mark.asyncio
    async def test_bounded_attempts_keep_honest_answer(self, monkeypatch):
        """始终空检索：总 workflow 调用 ≤ 1+2，改写 ≤2 次，最终保留诚实文案。"""
        monkeypatch.setattr(app_settings, "AGENTIC_RAG_ENABLED", True)
        workflow = _FakeWorkflow([{"answer": _HONEST, "sources": []}])
        model = _RewriteModel()
        recorder = _StubRecorder()
        _patch_kb_pipeline(monkeypatch, workflow, model, recorder)

        result = await general_handler.create_kb_query(_state(), config={})

        assert len(workflow.invocations) <= 1 + 2, "总步数必须有界"
        assert len(workflow.invocations) == 3
        assert len(model.ainvoke_calls) == 2, "改写重试 ≤2"
        assert len(recorder.records) == 2
        assert [r["detail"]["attempt"] for r in recorder.records] == [1, 2]
        assert all(r["status"] == "ok" for r in recorder.records)
        assert all(r["detail"]["hits"] == 0 for r in recorder.records)
        assert result["messages"][0].content == _HONEST, "仍空 ⇒ 诚实声明，不编造"

    @pytest.mark.asyncio
    async def test_rewrite_llm_failure_degrades_without_crash(self, monkeypatch):
        monkeypatch.setattr(app_settings, "AGENTIC_RAG_ENABLED", True)
        workflow = _FakeWorkflow([{"answer": _HONEST, "sources": []}])
        model = _RewriteModel(raise_exc=RuntimeError("rewrite backend down"))
        recorder = _StubRecorder()
        _patch_kb_pipeline(monkeypatch, workflow, model, recorder)

        result = await general_handler.create_kb_query(_state(), config={})

        assert len(workflow.invocations) == 1, "改写失败立即停止，不再空转"
        assert len(recorder.records) == 1
        assert recorder.records[0]["status"] == "error"
        assert "rewrite backend down" in recorder.records[0]["detail"]["error"]
        assert result["messages"][0].content == _HONEST, "降级返回最后一次诚实响应"

    @pytest.mark.asyncio
    async def test_trajectory_failure_is_swallowed(self, monkeypatch):
        """观测失败不影响主链路（懒 import + 全吞范式）。"""
        monkeypatch.setattr(app_settings, "AGENTIC_RAG_ENABLED", True)
        workflow = _FakeWorkflow([{"answer": _HONEST, "sources": []}])
        model = _RewriteModel()
        _patch_kb_pipeline(monkeypatch, workflow, model, _StubRecorder())

        def _boom():
            raise RuntimeError("trajectory backend down")

        monkeypatch.setattr(
            "agentix.application.services.trajectory.get_trajectory_recorder", _boom
        )

        result = await general_handler.create_kb_query(_state(), config={})

        assert len(workflow.invocations) == 3, "轨迹失败不得中断改写循环"
        assert result["messages"][0].content == _HONEST


# ---------------------------------------------------------------------------
# 6. 改写 prompt 外置（p23 三断言同款）
# ---------------------------------------------------------------------------


class TestRewritePromptExternalized:
    REL = "agentix/application/agents/general_handler.py"
    _FRAGMENT = "只输出改写后的查询"

    def test_literal_moved_out_of_handler(self):
        from pathlib import Path

        src = Path(self.REL).read_text(encoding="utf-8")
        assert self._FRAGMENT not in src, "prompt 字面量必须外置到 lg_prompts"
        assert "KB_QUERY_REWRITE_PROMPT" in src

    def test_constant_importable_and_nonempty(self):
        from agentix.application.agents.lg_prompts import KB_QUERY_REWRITE_PROMPT

        assert KB_QUERY_REWRITE_PROMPT.strip()
        assert self._FRAGMENT in KB_QUERY_REWRITE_PROMPT
        assert "{question}" in KB_QUERY_REWRITE_PROMPT

    def test_static_prefix_before_dynamic(self):
        from agentix.application.agents.lg_prompts import KB_QUERY_REWRITE_PROMPT

        template = KB_QUERY_REWRITE_PROMPT
        assert template.index(self._FRAGMENT) < template.index("{question}")

        rendered = template.format(question="违约金怎么算")
        assert rendered.index(self._FRAGMENT) < rendered.index("违约金怎么算")
        assert rendered.endswith("违约金怎么算"), "动态查询必须在静态指令之后"
