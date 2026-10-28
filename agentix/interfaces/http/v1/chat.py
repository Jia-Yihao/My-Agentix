"""
Unified Chat API with Agent Integration

Provides a single endpoint for chat interactions with automatic routing.

P1-6：业务编排（会话 CRUD、消息落库、agent 调用、流式事件、P0-3 错误分类）
已下沉到 application/services/chat_service.py；本层只保留
参数校验 → 调 service → 组装 HTTP 响应，不再出现任何 ORM 调用。
"""
import uuid
from typing import Any, Dict, List, Optional
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, status
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from agentix.config import settings
from agentix.application.services.breakpoint_resume import failure_boundary_content
from agentix.application.services.chat_service import (
    METADATA_EXCLUDE_KEYS,
    ChatService,
    get_chat_service,
)
from agentix.infrastructure.core.database import SessionLocal, get_db
from agentix.interfaces.http.middleware import trace_id_var
from agentix.interfaces.http.models.chat_message import ChatMessageResponse
from agentix.interfaces.http.v1.deps import (
    resolve_v1_caller,
    require_v1_user,
    session_visible_to,
)
from sqlalchemy.orm import Session

router = APIRouter(dependencies=[Depends(require_v1_user)])

# 编排层实例（模块级单例；graph 由 ChatService 惰性获取，规避 P0-1 值绑定陷阱）
_chat_service: ChatService = get_chat_service()

# P1-6 兼容锚点：P0-3 测试（test_p03_p04_errors_observability.py）通过
# `from agentix.interfaces.http.v1.chat import process_agent_query` 验证
# 错误分类行为；该行为已随编排下沉 ChatService.ask，此处保留别名零改动。
process_agent_query = _chat_service.ask


class ChatRequest(BaseModel):
    """Chat request model"""
    message: str = Field(
        ...,
        description="User message",
        min_length=1,
        max_length=settings.QUERY_MAX_LENGTH,  # 企业化 P2-4.5：统一上限
    )
    session_id: Optional[str] = Field(None, description="Session ID for conversation continuity")
    user_id: Optional[str] = Field("default_user", description="User identifier")
    stream: bool = Field(False, description="Enable streaming response")
    image_path: Optional[str] = Field(None, description="Path to uploaded image file")
    file_path: Optional[str] = Field(None, description="Path to uploaded file")
    ingest_incremental: Optional[bool] = Field(
        None,
        description="Override whether Excel ingestion uses incremental mode (defaults to server setting)",
    )


class ChatResponse(BaseModel):
    """Chat response model"""
    message: str
    session_id: str
    message_id: str
    route: Optional[str] = None
    route_logic: Optional[str] = None
    sources: Optional[List[Dict[str, Any]]] = None
    metadata: Optional[Dict[str, Any]] = None
    timestamp: datetime = Field(default_factory=datetime.now)
    # P0-3：结构化错误语义——前端据此区分「正常」/「系统降级」/「无结果」，
    # 而不是把降级兜底文案当成正常测试数据答复。
    status: str = Field(
        "ok",
        description="ok | degraded | no_evidence | out_of_scope",
    )
    error_code: Optional[str] = Field(None, description="P0-3 ErrorCode value, if any")
    # P0-4：贯穿 HTTP → Agent → LLM 的追踪 id（响应头 X-Trace-Id 同值）
    trace_id: Optional[str] = None


#: W0 性能戳记键：只有终帧带，非终帧从 wire 里整键剔除（帧形状不变）。
_PERF_FRAME_KEYS = ("duration_ms", "ttft_ms", "breakdown_ms")


class ChatStreamChunk(BaseModel):
    """Streaming response chunk"""

    type: str = Field(..., description="Chunk type: 'message', 'metadata', 'error', 'done'")
    # A5/R5b（SSE 词汇归一）：type 为唯一词汇；B2 双写窗口（event 字段）
    # 已关闭——前端 parser 自 B2 起已容错两词，弃 event 不致白屏。
    content: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    session_id: Optional[str] = None
    route: Optional[str] = None
    # A5/R5a（run-scoped emitter）：每帧带本 Run 内单调 seq + 登记成功的
    # run_id（守卫 #5：帧可回溯到统一 Run/Event stream；降级时为 None）。
    seq: Optional[int] = None
    run_id: Optional[str] = None
    # Phase H · W0 性能戳记：RunEventEmitter 只在**终帧**注入这三个键，非终帧为
    # None 且由 `_to_sse` 的 exclude 从 wire 里剔除（保持既有帧形状逐字节不变）。
    # 它们曾经在这里整个丢掉——DTO 字段集是白名单，序列化器不带就不出 wire，
    # 于是前端 `chatStreamHandlers.ts` 的耗时芯片永远拿不到数据。
    duration_ms: Optional[int] = None
    ttft_ms: Optional[int] = None
    breakdown_ms: Optional[Dict[str, int]] = None


@router.post("/", response_model=ChatResponse)
@router.post("", response_model=ChatResponse, include_in_schema=False)  # 无斜杠别名：前端直发 /api/v1/chat，避免 307 跳转丢 body
async def chat(
    request: ChatRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    caller: Optional[str] = Depends(resolve_v1_caller),
) -> ChatResponse:
    """
    Unified chat endpoint with automatic agent routing

    - Automatically routes queries to appropriate agents
    - Maintains conversation history
    - Supports file uploads and images
    """
    # 归属校验（审计批次 A1）：持身份者不得写入他人会话
    _ensure_session_visible(db, request.session_id, caller)
    # Get or create session（审计批次 A1：声明身份时 user_id 以身份为准）
    session_id = _chat_service.get_or_create_session(
        db, request.session_id, caller or request.user_id
    )

    # Save user message
    await _chat_service.save_message(db, session_id, request.message, is_user=True)

    # Process through agent
    effective_incremental = (
        request.ingest_incremental
        if request.ingest_incremental is not None
        else settings.INGEST_INCREMENTAL_DEFAULT
    )

    try:
        result = await _chat_service.ask(
            request.message,
            session_id,
            request.image_path,
            request.file_path,
            effective_incremental,
        )
    except HTTPException as exc:
        # P4-4.4 失败 turn durable 边界：**先持久化**边界消息，再继续抛出
        # （先持久化后发布——hermes cli_session_mixin.py:753）。flag-off ⇒
        # helper 首行返回，零写入，异常路径与基线逐位等价。
        raw_detail = exc.detail
        if isinstance(raw_detail, dict):
            boundary_code = str(raw_detail.get("error_code") or "").strip() or None
            boundary_fallback = str(raw_detail.get("message") or "")
        else:
            boundary_code = None
            boundary_fallback = str(raw_detail or "")
        await _persist_failure_boundary(
            db,
            session_id,
            error_code=boundary_code,
            fallback=boundary_fallback,
            status_code=exc.status_code,
        )
        raise

    # Save assistant message（sources 随 metadata 持久化——历史回放的
    # 来源引用卡不再丢失；save_message 按 METADATA_EXCLUDE_KEYS 过滤死重，
    # 其余键原样保留——A1d）
    assistant_meta = dict(result.get("metadata") or {})
    if result.get("sources") is not None:
        assistant_meta["sources"] = result.get("sources")
    message_id = await _chat_service.save_message(
        db,
        session_id,
        result["message"],
        is_user=False,
        route=result["route"],
        metadata=assistant_meta or None,
    )

    return ChatResponse(
        message=result["message"],
        session_id=session_id,
        message_id=message_id or str(uuid.uuid4()),
        route=result["route"],
        route_logic=result["route_logic"],
        sources=result.get("sources"),
        metadata=_public_metadata(result.get("metadata")),
        status=result.get("status", "ok"),
        error_code=result.get("error_code"),
        trace_id=trace_id_var.get(),
    )


# ---------------------------------------------------------------------------
# Legacy alias routes (backwards compatibility)
#
# Older docs/scripts use `/api/v1/chat/chat` and `/api/v1/chat/chat/stream`.
# Keep them working to reduce migration friction.
# ---------------------------------------------------------------------------


@router.post("/chat", response_model=ChatResponse, include_in_schema=False)
async def chat_legacy(
    request: ChatRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    caller: Optional[str] = Depends(resolve_v1_caller),
) -> ChatResponse:
    return await chat(request, background_tasks, db, caller)


def _public_metadata(meta: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """响应侧与落库口径对齐（接口审查批次 2026-09-28；A1d 扩集）：
    与 ``save_message`` 共用 ``METADATA_EXCLUDE_KEYS`` 死重过滤集
    （agent_state / runtime / budget / completion）——不把整图状态与
    观测负载外泄给客户端，同时省掉数 KB 的冗余响应体。"""
    if not isinstance(meta, dict):
        return meta
    return {k: v for k, v in meta.items() if k not in METADATA_EXCLUDE_KEYS}


async def _persist_failure_boundary(
    db: Session,
    session_id: str,
    *,
    error_code: Optional[str],
    fallback: str,
    status_code: Optional[int] = None,
) -> bool:
    """P4-4.4：失败 turn 的 assistant 边界补写（**先持久化**，调用方随后
    抛出/发布——见各调用点注释）。

    - ``FAILURE_BOUNDARY_ENABLED=false``（默认）⇒ 首行返回 ``False``，零写入
      （与基线逐位等价：ask 失败时历史 user-user 相邻是既有行为）；
    - 排除码（context_overflow）⇒ ``None`` 文案 ⇒ 不补写（见
      ``breakpoint_resume.failure_boundary_content``）；
    - 补写自身失败**绝不抛出**——边界是一致性修复，不覆盖原始错误路径
      （``save_message`` 内部也已吞异常返回 None，此处再兜一层）。
    """
    if not bool(getattr(settings, "FAILURE_BOUNDARY_ENABLED", False)):
        return False
    content = failure_boundary_content(error_code, fallback=fallback)
    if not content:
        return False
    try:
        await _chat_service.save_message(
            db,
            session_id,
            content,
            is_user=False,
            route="error",
            metadata={
                "failure_boundary": True,
                "error_code": error_code or "unknown_error",
                **({"http_status": status_code} if status_code else {}),
            },
        )
    except Exception as persist_exc:  # noqa: BLE001 - 边界修复不覆盖原始错误
        logger.warning(
            "failure boundary persist failed (non-fatal): {}", persist_exc
        )
    return True


def _ensure_session_visible(
    db: Session, session_id: Optional[str], caller: Optional[str]
) -> None:
    """写路径归属校验（审计批次 A1）。

    ``session_id`` 给定且会话存在时，跨用户写入（往他人会话投消息）回 404；
    会话不存在保持既有语义（``get_or_create_session`` 走新建分支）。
    """
    if not session_id or caller is None:
        return
    if not _chat_service.session_exists(db, session_id):
        return
    owner = _chat_service.get_session_owner(db, session_id)
    if not session_visible_to(owner, caller):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )


def _to_sse(event: Dict[str, Any]) -> str:
    """业务事件 dict → SSE 帧（service 产出业务语义，本层负责传输格式）。"""
    perf = {name: event.get(name) for name in _PERF_FRAME_KEYS}
    chunk = ChatStreamChunk(
        type=event["type"],
        content=event.get("content"),
        metadata=event.get("metadata"),
        session_id=event.get("session_id"),
        route=event.get("route"),
        seq=event.get("seq"),
        run_id=event.get("run_id"),
        **perf,
    )
    # 非终帧逐字节保持原样：没带的性能键连 null 都不补
    drop = {name for name, value in perf.items() if value is None}
    return f"data: {chunk.model_dump_json(exclude=drop)}\n\n"


@router.post("/stream")
async def chat_stream(
    request: ChatRequest,
    db: Session = Depends(get_db),
    caller: Optional[str] = Depends(resolve_v1_caller),
) -> StreamingResponse:
    """
    Streaming chat endpoint with automatic agent routing

    Returns responses in Server-Sent Events (SSE) format
    """
    # 归属校验（审计批次 A1）+ 身份优先的 user_id
    _ensure_session_visible(db, request.session_id, caller)
    session_id = _chat_service.get_or_create_session(
        db, request.session_id, caller or request.user_id
    )

    # Save user message
    await _chat_service.save_message(db, session_id, request.message, is_user=True)

    effective_incremental = (
        request.ingest_incremental
        if request.ingest_incremental is not None
        else settings.INGEST_INCREMENTAL_DEFAULT
    )

    async def event_stream() -> Any:
        # 接口审查批次 2026-09-28（流式主路径落库）：此前流式端点只保存
        # 用户消息，助手回答从不落库——而 ChatWidget 主路径就是 /stream，
        # 导致刷新后 restoreHistory 只剩用户提问、引用来源全丢。
        # 现在缓冲整段正文 + route + sources，生成器收尾（正常结束、异常、
        # 客户端 aclose 断连均会走到 finally）时用**独立会话**落库一次。
        persist_db = SessionLocal()
        answer_parts: List[str] = []
        final_route: Optional[str] = None
        final_sources: Optional[List[Any]] = None
        boundary_saved = False
        try:
            from agentix.application.services.stream_gate import gate_events

            # Phase H·W4：SSE 出口门控（token 50ms 合并/有界队列背压，
            # flag 关 = 原 source 直通逐位等价）。挂 HTTP 出口：service 层
            # 帧语义/戳记/落库逻辑（下方循环）不受影响，合并帧 content
            # 为拼接 delta ⇒ answer_parts 落库内容恒等。
            async for event in gate_events(
                _chat_service.stream_events(
                    request.message,
                    session_id,
                    request.image_path,
                    request.file_path,
                    effective_incremental,
                )
            ):
                etype = event.get("type")
                if etype == "message" and event.get("content"):
                    answer_parts.append(str(event["content"]))
                elif etype == "metadata" and event.get("route"):
                    final_route = str(event["route"])
                elif etype in ("done", "error"):
                    meta = event.get("metadata")
                    if isinstance(meta, dict) and isinstance(meta.get("sources"), list):
                        final_sources = meta["sources"]
                if etype == "error" and not answer_parts and not boundary_saved:
                    # P4-4.4 失败 turn durable 边界：正文为空（finally 不会
                    # 落库）时**先持久化**边界消息，再 yield error 帧
                    # （先持久化后发布；flag-off ⇒ helper 零写入）。
                    err_meta = event.get("metadata")
                    err_code: Optional[str] = None
                    if isinstance(err_meta, dict):
                        err_code = (
                            str(err_meta.get("error_code") or "").strip() or None
                        )
                    boundary_saved = await _persist_failure_boundary(
                        persist_db,
                        session_id,
                        error_code=err_code,
                        fallback=str(event.get("content") or ""),
                    )
                yield _to_sse(event)
        finally:
            content = "".join(answer_parts)
            if content:
                try:
                    await _chat_service.save_message(
                        persist_db,
                        session_id,
                        content,
                        is_user=False,
                        route=final_route,
                        metadata={"sources": final_sources}
                        if final_sources is not None
                        else None,
                    )
                except Exception as persist_exc:  # noqa: BLE001 - 落库失败不影响事件流
                    logger.warning(
                        "stream assistant message persist failed (non-fatal): {}",
                        persist_exc,
                    )
            try:
                persist_db.close()
            except Exception:  # noqa: BLE001
                pass

    # Return streaming response
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"  # Disable nginx buffering
        }
    )


@router.post("/chat/stream", include_in_schema=False)
async def chat_stream_legacy_post(
    request: ChatRequest,
    db: Session = Depends(get_db),
    caller: Optional[str] = Depends(resolve_v1_caller),
) -> StreamingResponse:
    return await chat_stream(request, db, caller)


@router.get("/chat/stream", include_in_schema=False)
async def chat_stream_legacy_get(
    # 接口审查批次 2026-09-28：原硬编码 max_length=5000，与 ChatRequest 的
    # settings.QUERY_MAX_LENGTH 口径分叉（改配置后 GET/POST 上限不一致）。
    message: str = Query(..., min_length=1, max_length=settings.QUERY_MAX_LENGTH),
    session_id: Optional[str] = Query(None),
    user_id: Optional[str] = Query("default_user"),
    image_path: Optional[str] = Query(None),
    file_path: Optional[str] = Query(None),
    ingest_incremental: Optional[bool] = Query(None),
    db: Session = Depends(get_db),
    caller: Optional[str] = Depends(resolve_v1_caller),
) -> StreamingResponse:
    request = ChatRequest(
        message=message,
        session_id=session_id,
        user_id=caller or user_id,
        stream=True,
        image_path=image_path,
        file_path=file_path,
        ingest_incremental=ingest_incremental,
    )
    return await chat_stream(request, db, caller)


@router.get("/history/{session_id}", response_model=List[ChatMessageResponse])
async def get_chat_history(
    session_id: str,
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    caller: Optional[str] = Depends(resolve_v1_caller),
) -> List[ChatMessageResponse]:
    """
    Get chat history for a session（最近 N 条，正序返回）。

    接口审查批次 2026-09-28：
    - ``limit`` 补 ``ge=1`` 下界——此前 ``limit=-1`` 在 SQLite 上等价全量
      （PostgreSQL 则直接语法错 500），``limit=0`` 返回空数组让前端静默不恢复；
    - 改用 ``list_recent_messages``：窗口从**最新**往回数，会话超过 limit 时
      保留最新问答（原实现丢的恰是最新的）。

    审计批次 A1（2026-09-28）：跨用户读他人会话 → 404（防存在性枚举）；
    匿名（无身份声明）行为不变。
    """
    # Verify session exists + 归属校验（审计批次 A1）
    if not _chat_service.session_exists(db, session_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )
    if not session_visible_to(
        _chat_service.get_session_owner(db, session_id), caller
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )

    # Get messages
    return _chat_service.list_recent_messages(
        db, session_id=session_id, skip=offset, limit=limit
    )


@router.delete("/session/{session_id}")
async def clear_session(
    session_id: str,
    db: Session = Depends(get_db),
    caller: Optional[str] = Depends(resolve_v1_caller),
) -> Dict[str, str]:
    """
    Clear all messages in a session
    """
    if not _chat_service.session_exists(db, session_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )
    if not session_visible_to(
        _chat_service.get_session_owner(db, session_id), caller
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )

    # Delete all messages in session
    _chat_service.clear_session_messages(db, session_id=session_id)

    return {"message": "Session cleared successfully", "session_id": session_id}


@router.get("/routes")
async def get_route_info() -> Dict[str, Any]:
    """
    Get information about available routes and their purposes
    """
    return {
        "routes": {
            "general-query": {
                "name": "日常对话",
                "description": "处理问候、寒暄等日常对话",
                "examples": ["你好", "谢谢", "今天天气不错"]
            },
            "additional-query": {
                "name": "补充信息",
                "description": "当问题模糊时，询问更多信息",
                "examples": ["我想查测试数据集里的内容", "帮我推荐相关的数据条目"]
            },
            "kb-query": {
                "name": "知识库查询",
                "description": "查询条目释义、数据来源背景与配套说明等内容",
                "examples": ["「生效日期」这个字段是什么意思", "示例数据集 A 是怎么构建的"]
            },
            "graphrag-query": {
                "name": "图谱查询",
                "description": "查询数据条目内容、分类适用范围与示例说明",
                "examples": ["示例数据集 A 的第 577 条讲了什么", "哪些数据条目适用于示例文档异常场景"]
            },
            "text2sql-query": {
                "name": "统计查询",
                "description": "统计分析、计数、排名（测试数据统计库暂缓启用）",
                "examples": ["一共有多少个数据分类", "示例数据集 B 有多少条数据条目"]
            },
            "image-query": {
                "name": "图片处理",
                "description": "生成或分析测试数据主题的图片",
                "examples": ["生成一张示例数据集 A 主题的图片"]
            },
            "file-query": {
                "name": "文件处理",
                "description": "处理上传的测试文档",
                "examples": ["分析这个测试文档"]
            }
        },
        "auto_routing": "系统会根据您的问题自动选择合适的处理方式"
    }
