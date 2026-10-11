"""P2 可解释（X2）+ 可观测（O1）：答案解释 API 与业务质量指标测试。

覆盖：
- GET /answers/{id}/explanation 正常路径（问题/证据/引用/模型聚合）
- 无检索运行（拒答回答）→ evidence.status == "no_retrieval"
- 未知 answer → 404 ANSWER_NOT_FOUND
- validation 各块显式 not_recorded（不编造）
- O1：record_grounding_low_score / record_gateway_fallback 计数器递增
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from agentix.retrieval.api.deps import get_current_user
from agentix.retrieval.api.response import get_status_code
from agentix.retrieval.api.routers.explanations import router as explanations_router
from agentix.retrieval.domain.models.chat import Message
from agentix.retrieval.domain.models.index import Answer, RetrievalRun
from agentix.retrieval.errors import AppError
from agentix.retrieval.infra.db.session import get_session


@pytest.fixture()
def db_session():
    """内存 sqlite + 仅创建本测试涉及的表（零外部依赖）。

    StaticPool：单连接共享——TestClient 在工作线程执行请求，
    默认池会按线程各给一个空内存库，导致 create_all 的表"消失"。
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(
        engine, tables=[Answer.__table__, RetrievalRun.__table__, Message.__table__]
    )
    with Session(engine) as session:
        yield session


@pytest.fixture()
def client(db_session):
    """仅挂 explanations 路由的极简 app（覆写 session 与认证依赖）。

    AppError 处理器按 get_status_code 语义映射——与 authz.py 的 AppError
    约定一致（全局转换器是平台级既有议题，不在本测试范围）。
    """
    app = FastAPI()
    app.include_router(explanations_router)

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.status_code or get_status_code(exc.error_code),
            content={
                "request_id": getattr(request.state, "request_id", ""),
                "error_code": exc.error_code,
                "message": exc.message,
            },
        )

    @app.middleware("http")
    async def _set_request_id(request, call_next):
        request.state.request_id = "test-rid"
        return await call_next(request)

    app.dependency_overrides[get_session] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="u-test")
    with TestClient(app) as test_client:
        yield test_client


def _seed_answer(db_session: Session, *, with_retrieval: bool) -> Answer:
    msg = Message(
        conversation_id="c1", role="user", content="劳动合同违约金上限是多少？"
    )
    db_session.add(msg)
    db_session.commit()
    db_session.refresh(msg)

    if with_retrieval:
        db_session.add(
            RetrievalRun(
                kb_id="kb1",
                conversation_id="c1",
                question_message_id=msg.id,
                query_text="劳动合同 违约金 上限",
                top_k=5,
                hit_chunks=[
                    {
                        "chunk_id": "chunk-1",
                        "score": 0.91,
                        "content": "劳动合同法第二十五条：除本法第二十二条和第二十三条规定的情形外……" + "x" * 300,
                        "source": "劳动合同法",
                        "effective_status": "in_force",
                    },
                    {
                        "chunk_id": "chunk-2",
                        "score": 0.72,
                        "content": "相关司法解释节选……",
                        "source": "司法解释",
                        "effective_status": "expired",
                    },
                ],
            )
        )
        db_session.commit()

    answer = Answer(
        conversation_id="c1",
        question_message_id=msg.id,
        answer_text="根据《劳动合同法》第二十五条……",
        citations=(
            [
                {"chunk_id": "chunk-1", "source": "劳动合同法", "effective_status": "in_force"}
            ]
            if with_retrieval
            else []
        ),
        model_name="qwen3-max",
    )
    db_session.add(answer)
    db_session.commit()
    db_session.refresh(answer)
    return answer


class TestExplanationApi:
    def test_explanation_aggregates_persisted_data(self, client, db_session):
        """正常路径：问题/证据/引用/模型聚合，validation 显式 not_recorded。"""
        answer = _seed_answer(db_session, with_retrieval=True)

        resp = client.get(f"/answers/{answer.id}/explanation")
        assert resp.status_code == 200
        data = resp.json()["data"]

        # 问题聚合
        assert data["question"]["content"] == "劳动合同违约金上限是多少？"
        assert data["question"]["message_id"] == answer.question_message_id
        # 模型名已记录，prompt_version 未持久化
        assert data["model"]["name"] == "qwen3-max"
        assert data["model"]["prompt_version"]["status"] == "not_recorded"
        # 证据：query/top_k/命中块（含 score 与效力状态，内容预览截断）
        evidence = data["evidence"]
        assert evidence["status"] == "recorded"
        run = evidence["retrieval_run"]
        assert run["query_text"] == "劳动合同 违约金 上限"
        assert run["top_k"] == 5
        assert run["hit_count"] == 2
        first = run["chunks"][0]
        assert first["chunk_id"] == "chunk-1"
        assert first["score"] == 0.91
        assert first["effective_status"] == "in_force"
        assert len(first["content_preview"]) <= 160
        # 引用与 validation
        assert data["citations"][0]["chunk_id"] == "chunk-1"
        assert set(data["validation"]) == {
            "route_decision",
            "citation_verifier",
            "grounding",
            "prompt_version",
        }
        assert all(v["status"] == "not_recorded" for v in data["validation"].values())

    def test_explanation_without_retrieval_is_no_retrieval(self, client, db_session):
        """无检索运行的回答（如拒答）也是合法解释：evidence=no_retrieval。"""
        answer = _seed_answer(db_session, with_retrieval=False)

        resp = client.get(f"/answers/{answer.id}/explanation")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["evidence"]["status"] == "no_retrieval"
        assert data["citations"] == []

    def test_unknown_answer_returns_404(self, client):
        resp = client.get("/answers/nonexistent/explanation")
        assert resp.status_code == 404
        body = resp.json()
        assert body.get("error_code") == "ANSWER_NOT_FOUND" or "ANSWER_NOT_FOUND" in str(body)


class TestO1BusinessMetrics:
    def test_record_grounding_low_score_increments_counter(self):
        from agentix.observability.metrics import record_grounding_low_score

        from prometheus_client import REGISTRY

        before = REGISTRY.get_sample_value("grounding_low_score_total") or 0.0
        record_grounding_low_score()
        after = REGISTRY.get_sample_value("grounding_low_score_total") or 0.0
        assert after == before + 1

    def test_record_gateway_fallback_increments_labeled_counter(self):
        from agentix.observability.metrics import record_gateway_fallback

        from prometheus_client import REGISTRY

        labels = {"primary_model": "gw-test-primary", "fallback_model": "gw-test-fallback"}
        before = REGISTRY.get_sample_value("gateway_fallback_total", labels) or 0.0
        record_gateway_fallback("gw-test-primary", "gw-test-fallback")
        after = REGISTRY.get_sample_value("gateway_fallback_total", labels) or 0.0
        assert after == before + 1
