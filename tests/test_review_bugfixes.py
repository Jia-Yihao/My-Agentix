"""接口审查修复回归（2026-09-26 审查批次）。

钉死四处修复：

1. ``AgentLoop`` 异常分支 import 错包（旧从 capabilities import
   ``classify_exception``，符号不在该模块 → except 内再抛 ImportError 逸出
   ``run()``，把工具失败变成未捕获崩溃）；
2. ``ToolExecutor._mirror_trajectory`` / ``AgentRuntime._mirror_trajectory``
   误用 ``begin_step``（丢弃返回值 + ``steps.pop()`` 弹出上一条已完成步）：
   新步不入轨迹、历史步被覆写；
3. ``EventPort`` 协议补齐 webhook 实际调用的 kwargs（event_key/occurred_at/ttl_s/poll_s）；
4. ``POST /api/v1/chat`` 无斜杠别名（原先 307 跳转，代理下偶发丢 body）；
5. ``GET /api/v1/sessions/`` 的 ``user_id`` 改必填（原先可缺省 → 无鉴权下
   任何人可枚举全量会话时间线）；
6. ``DocumentData`` 补 ``updated_at``（模型有列、DTO 漏带 → 前端恒 ``undefined``）。
"""

from __future__ import annotations

import inspect

import pytest

from agentix.agent import AgentLoop
from agentix.agent.context import SimpleContextBuilder
from agentix.policies.planner import SingleStepPlanner
from agentix.domain.agent.types import FailureLayer, TaskSpec
from agentix.domain.agent.ports import EventPort
from agentix.observability.trajectory import (
    TrajectoryRecorder,
    reset_recorder,
    set_recorder,
)


class _AlwaysPassVerifier:
    async def verify(self, task, plan, observations):
        from agentix.domain.agent.types import VerificationResult

        return VerificationResult(passed=True, reason="always")


class _RaisingExecutor:
    """自定义 executor 直接抛异常（MappingStepExecutor 内部已吞，覆盖不到 loop 的 except 分支）。"""

    async def execute(self, task, plan, step, context):
        raise RuntimeError("upstream 503 error")


# ---------------------------------------------------------------------------
# 1. loop except 分支 import 修复
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_raising_executor_classifies_without_import_error():
    loop = AgentLoop(
        planner=SingleStepPlanner(),
        context=SimpleContextBuilder(),
        verifier=_AlwaysPassVerifier(),
        executor=_RaisingExecutor(),
        max_steps=5,
    )
    result = await loop.run(TaskSpec(goal="g", task_id="t-raise"))
    assert result.observations, "异常必须归一为 Observation"
    obs = result.observations[0]
    assert obs.ok is False
    assert obs.error_code == "tool_exec_failed"
    # classify_exception 按消息分层：含 "503" → API 层（import 修复后才能算出）
    assert obs.failure_layer is FailureLayer.API


# ---------------------------------------------------------------------------
# 2/3. 轨迹镜像 begin_step 修复
# ---------------------------------------------------------------------------


def test_tool_mirror_appends_new_step_and_preserves_history():
    from agentix.tools.gateway.executor import ToolExecutor, ToolResult

    rec = TrajectoryRecorder(name="t")
    prior = rec.begin_step("llm.router")
    rec.end_step(prior, status="ok")

    token = set_recorder(rec)
    try:
        ToolExecutor()._mirror_trajectory(
            "search", ToolResult(tool_name="search", success=True, latency_ms=5)
        )
    finally:
        reset_recorder(token)

    # 修复前：新步不入列 + 上一条 llm.router 被 pop 出来覆写成 tool.search
    assert [s["name"] for s in rec.steps] == ["llm.router", "tool.search"]
    assert rec.steps[0]["status"] == "ok"
    assert rec.steps[1]["status"] == "ok"
    assert rec.steps[1]["attributes"]["latency_ms"] == 5
    assert "call_id" in rec.steps[1]["attributes"]
    assert rec.steps[1]["seq"] == 2
    assert rec.failure_stage is None


def test_tool_mirror_error_step_on_empty_recorder_keeps_name():
    from agentix.tools.gateway.executor import ToolExecutor, ToolResult

    rec = TrajectoryRecorder(name="t")
    token = set_recorder(rec)
    try:
        ToolExecutor()._mirror_trajectory(
            "search", ToolResult(tool_name="search", success=False, error="boom")
        )
    finally:
        reset_recorder(token)

    # 修复前：空 steps → end_step({}) 后 failure_stage 赋值 KeyError → 整步静默丢失
    assert len(rec.steps) == 1
    assert rec.steps[0]["name"] == "tool.search"
    assert rec.steps[0]["status"] == "error"
    assert rec.failure_stage == "tool.search"


def test_runtime_mirror_appends_new_step_and_preserves_history():
    from agentix.runtime.runtime import AgentRuntime
    from agentix.domain.runtime.steps import StepType
    from agentix.runtime.steps import ExecutionStep

    rec = TrajectoryRecorder(name="t")
    prior = rec.begin_step("llm.router")
    rec.end_step(prior, status="ok")

    exec_step = ExecutionStep(step_type=StepType.TOOL, name="legal_search")
    exec_step.complete()

    token = set_recorder(rec)
    try:
        AgentRuntime()._mirror_trajectory(exec_step)
    finally:
        reset_recorder(token)

    assert [s["name"] for s in rec.steps] == [
        "llm.router",
        "runtime.tool.legal_search",
    ]
    assert rec.steps[0]["status"] == "ok"
    assert rec.steps[1]["status"] == "ok"
    assert rec.steps[1]["attributes"]["step_id"] == exec_step.step_id


# ---------------------------------------------------------------------------
# 4. EventPort 协议与 webhook 调用面对齐
# ---------------------------------------------------------------------------


class TestEventPortContract:
    def test_protocol_declares_webhook_kwargs(self):
        publish_params = inspect.signature(EventPort.publish).parameters
        for name in ("event_key", "occurred_at", "ttl_s", "max_retries"):
            assert name in publish_params, f"协议缺 webhook 依赖的 kwarg: {name}"
        wait_params = inspect.signature(EventPort.wait).parameters
        assert "timeout_s" in wait_params
        assert "poll_s" in wait_params

    def test_implementations_satisfy_protocol(self):
        import tempfile
        from pathlib import Path

        from agentix.agent.events.adapter import InMemoryEventBus
        from agentix.agent.events.jsonl import JsonlEventBus

        with tempfile.TemporaryDirectory() as tmp:
            jsonl = JsonlEventBus(Path(tmp) / "events.jsonl")
            assert isinstance(jsonl, EventPort)
        assert isinstance(InMemoryEventBus(), EventPort)


# ---------------------------------------------------------------------------
# 5. POST /api/v1/chat 无斜杠别名
# ---------------------------------------------------------------------------


def test_chat_has_both_slash_variants():
    from fastapi import FastAPI

    from agentix.interfaces.http.v1 import api_router
    from route_introspect import effective_paths

    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    paths = set(effective_paths(app.routes))
    assert "/api/v1/chat" in paths, "前端直发 /api/v1/chat（无斜杠）需要直接命中"
    assert "/api/v1/chat/" in paths, "既有带斜杠路径保留（兼容）"


# ---------------------------------------------------------------------------
# 6. GET /api/v1/sessions/ user_id 必填（防全量枚举）
# ---------------------------------------------------------------------------


def test_sessions_list_requires_user_id():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from agentix.interfaces.http.v1 import api_router

    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/api/v1/sessions/")
    assert resp.status_code == 422, "缺 user_id 必须被参数校验拒绝（不可静默返回全量）"

    # 带 user_id 时请求必须放行到业务层（离线环境 DB 可能 500，但绝不能 422）
    resp2 = client.get("/api/v1/sessions/", params={"user_id": "u-review"})
    assert resp2.status_code != 422


# ---------------------------------------------------------------------------
# 7. DocumentData 补 updated_at
# ---------------------------------------------------------------------------


def test_document_data_carries_updated_at():
    from agentix.retrieval.api.schemas.documents import DocumentData
    from agentix.retrieval.domain.models.document import Document
    from agentix.retrieval.usecases.document_mutation import _to_doc_data

    doc = Document(kb_id="kb-review", title="a.pdf")
    data = _to_doc_data(doc)
    assert data["updated_at"] == doc.updated_at
    # schema 反序列化同样保留（前端 documents.ts updated_at?: string 对齐）
    payload = DocumentData(**data)
    assert payload.updated_at == doc.updated_at
