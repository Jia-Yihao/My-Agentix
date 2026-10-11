"""企业化 P2-4.5：长文本输入统一上限（QUERY_MAX_LENGTH）测试。

覆盖四个 HTTP 入口 DTO：编排域 v1/chat、旧版 models/chat、
检索域 schemas/chat、LightRAG 查询——超限必须 422 拒绝，
防 token 放大攻击。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentix.config.settings import settings

OVER_LIMIT_TEXT = "长" * (settings.QUERY_MAX_LENGTH + 1)
OK_TEXT = "民法典关于合同违约是怎么规定的？"


def _assert_rejected_for_length(model_cls, field_name: str, **kwargs) -> None:
    """超限必须**因『字段太长』**被拒。

    原写法是 ``pytest.raises(Exception)``——任何异常都能让断言通过，
    哪怕模型是因为别的原因（字段名拼错、缺必填项）才抛错，
    测试照样是绿的，等于没测到长度上限。这里收紧到
    ``ValidationError`` **且**错误定位在该字段、错误类型为 ``string_too_long``。
    """
    with pytest.raises(ValidationError) as excinfo:
        model_cls(**kwargs)
    errors = excinfo.value.errors()
    assert any(
        err["loc"] == (field_name,) and err["type"] == "string_too_long"
        for err in errors
    ), f"{model_cls.__name__}.{field_name} 超限被拒的原因不是 string_too_long：{errors}"


def test_settings_query_max_length_positive():
    assert settings.QUERY_MAX_LENGTH > 0


def test_v1_chat_request_rejects_over_limit():
    """编排域统一入口：v1/chat.ChatRequest。"""
    from agentix.interfaces.http.v1.chat import ChatRequest

    _assert_rejected_for_length(ChatRequest, "message", message=OVER_LIMIT_TEXT)

    req = ChatRequest(message=OK_TEXT)
    assert req.message == OK_TEXT


def test_legacy_models_chat_request_rejects_over_limit():
    """旧版 models/chat.ChatRequest（保持旧入口同口径）。"""
    from agentix.interfaces.http.models.chat import ChatRequest

    _assert_rejected_for_length(ChatRequest, "message", message=OVER_LIMIT_TEXT)

    assert ChatRequest(message=OK_TEXT).message == OK_TEXT


def test_retrieval_chat_request_rejects_over_limit():
    """检索域问答入口：schemas/chat.ChatRequest.question。"""
    from agentix.retrieval.api.schemas.chat import ChatRequest

    _assert_rejected_for_length(
        ChatRequest, "question", kb_id="kb-1", question=OVER_LIMIT_TEXT
    )

    req = ChatRequest(kb_id="kb-1", question=OK_TEXT)
    assert req.question == OK_TEXT


def test_lightrag_query_request_rejects_over_limit():
    """LightRAG 查询入口：LightRAGQueryRequest.query。"""
    from agentix.application.services.lightrag_service import LightRAGQueryRequest

    _assert_rejected_for_length(
        LightRAGQueryRequest, "query", query=OVER_LIMIT_TEXT
    )

    assert LightRAGQueryRequest(query=OK_TEXT).query == OK_TEXT


def test_chat_entry_length_cap_is_documented_list():
    """清单化自检：四个入口 DTO 的上限值必须与 settings 同源（防漂移）。"""
    from agentix.interfaces.http.models.chat import ChatRequest as LegacyChatRequest
    from agentix.interfaces.http.v1.chat import ChatRequest as V1ChatRequest
    from agentix.retrieval.api.schemas.chat import ChatRequest as RetrievalChatRequest
    from agentix.application.services.lightrag_service import LightRAGQueryRequest

    for model, field_name in (
        (V1ChatRequest, "message"),
        (LegacyChatRequest, "message"),
        (RetrievalChatRequest, "question"),
        (LightRAGQueryRequest, "query"),
    ):
        max_len = model.model_fields[field_name].metadata[-1].max_length
        assert max_len == settings.QUERY_MAX_LENGTH, (
            f"{model.__name__}.{field_name} 上限与 QUERY_MAX_LENGTH 不同源"
        )
