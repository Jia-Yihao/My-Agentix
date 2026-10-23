"""
File handling module - file upload and ingestion.
"""

import uuid
import json
from typing import Dict, List

import aiohttp
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableConfig
from loguru import logger

from agentix.application.agents.constants import (
    FILE_NOT_FOUND_MESSAGE,
    FILE_TOO_LARGE_MESSAGE,
    FILE_TYPE_UNSUPPORTED_MESSAGE,
    FILE_INGEST_ERROR_MESSAGE,
    FILE_TEXT_INGEST_UNAVAILABLE_MESSAGE,
    FILE_PDF_USE_DOCUMENT_API_MESSAGE,
)
from agentix.orchestration.langgraph.lg_states import AgentState
from agentix.application.agents.kb_tools import create_knowledge_query_node, KnowledgeQueryInputState
from agentix.application.agents.router import _extract_configurable, _coerce_to_bool
from agentix.config import settings
from agentix.domain.policies.ingest_auth import ingest_auth_headers_for
from agentix.domain.policies.upload_paths import resolve_within_root
from agentix.application.services.knowledge_gateway import KnowledgeGateway, get_retrieval_port


async def create_file_query(state: AgentState, *, config: RunnableConfig) -> Dict[str, List[BaseMessage]]:
    """Handle user-provided files for ingestion."""
    logger.info("-----Found User Upload File-----")
    config_opts = _extract_configurable(config)
    file_path = config_opts.get("file_path")
    ingest_service_url = settings.INGEST_SERVICE_URL

    if not file_path:
        logger.warning("User Upload File Path is None")
        return {"messages": [AIMessage(content="请提供要处理的文件路径。")]}

    # 2026-09（P43）：``file_path`` 直接来自聊天请求体/查询参数
    # （``agentix/interfaces/http/v1/chat.py`` 的 ``ChatRequest.file_path``），
    # 完全由客户端控制。此前这里直接 ``Path(file_path).read_text()`` ——
    # 配合默认关闭的 ``REQUIRE_API_KEY``，等于一个**任意文件读取**：
    # 传 ``/path/to/.env`` 或 SSH 私钥（≤ FILE_UPLOAD_MAX_MB，默认 2MB）即可读到内容，
    # 内容随后进入知识库/LLM 并回给调用方。
    # 合法调用方的 ``file_path`` 只会是上传接口的返回值（``_safe_resolve`` 产物），
    # 必在 ``settings.UPLOAD_DIR`` 之内——所以这里做包含性约束**不改变正常流程**。
    #
    # 校验刻意放在 ``KnowledgeGateway()`` **之前**：被拒的请求不该去建网关
    # （fail fast），同时也让这段判定能被单测直接覆盖而无需重型依赖。
    safe_path = resolve_within_root(settings.UPLOAD_DIR, file_path)
    if safe_path is None:
        logger.warning(
            "Rejected out-of-root file_path (must be under UPLOAD_DIR=%s): %r",
            settings.UPLOAD_DIR, file_path,
        )
        return {"messages": [AIMessage(content=FILE_NOT_FOUND_MESSAGE)]}

    p = safe_path
    if not p.exists() or not p.is_file():
        logger.warning("User Upload File Not Found: %s", file_path)
        return {"messages": [AIMessage(content=FILE_NOT_FOUND_MESSAGE)]}

    try:
        suffix = p.suffix.lower()
        size_bytes = p.stat().st_size
        if size_bytes > settings.FILE_UPLOAD_MAX_MB * 1024 * 1024:
            return {"messages": [AIMessage(content=FILE_TOO_LARGE_MESSAGE.format(max_mb=settings.FILE_UPLOAD_MAX_MB))]}

        if suffix in {".xlsx", ".xls"}:
            if not ingest_service_url:
                return {"messages": [AIMessage(content="未配置外部接入服务 INGEST_SERVICE_URL，无法处理 Excel 导入。")]}
            incremental_flag = _coerce_to_bool(
                config_opts.get("incremental"),
                default=settings.INGEST_INCREMENTAL_DEFAULT,
            )
            regenerate_flag = _coerce_to_bool(config_opts.get("regenerate"), default=False)
            payload = {
                "excel_path": str(p),
                "incremental": incremental_flag,
                "regenerate": regenerate_flag,
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{ingest_service_url.rstrip('/')}/api/ingest/excel",
                    json=payload,
                    # kb_ingest 现在要求 X-API-Key（REQUIRE_API_KEY=true 时）。
                    # 不带上就会 401，而调用点会把它当"外部服务请求失败"报给用户。
                    headers=ingest_auth_headers_for(
                        ingest_service_url,
                        api_key=settings.KB_API_KEY,
                        ingest_service_url=settings.INGEST_SERVICE_URL,
                    ),
                    timeout=60,
                ) as resp:
                    if resp.status not in (200, 202):
                        txt = await resp.text()
                        return {"messages": [AIMessage(content=f"外部 Excel 导入请求失败（{resp.status}）：{txt}")]}
            return {"messages": [AIMessage(content="已启动 Excel 导入（外部服务），完成后可直接检索或提问。")]}

        raw_text = ""
        if suffix in {".txt", ".md", ".csv", ".log"}:
            raw_text = p.read_text(encoding="utf-8", errors="ignore")
        elif suffix == ".json":
            data = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
            raw_text = json.dumps(data, ensure_ascii=False, indent=2)
        elif suffix == ".pdf":
            # PDF 不是"不支持"，而是**走错了入口**：入库管线（切分 → 向量化 →
            # 索引）挂在检索域的文档管理接口上。这里必须给准确指引，否则
            # 平台主业（测试数据 PDF）的用户会在这里被告知"不支持 .pdf"。
            return {"messages": [AIMessage(content=FILE_PDF_USE_DOCUMENT_API_MESSAGE)]}
        else:
            return {"messages": [AIMessage(content=FILE_TYPE_UNSUPPORTED_MESSAGE.format(suffix=suffix))]}

        doc_id = f"upload_{p.stem}_{uuid.uuid4().hex[:6]}"
        title = p.stem
        # 网关**延迟到真正要入库时**才构造：上面所有早退分支（类型不支持、
        # 走错入口、超限）都不该付出建网关的代价。原先把构造放在后缀分派
        # 之前，等于给每一个"注定被拒"的请求也建一次网关。
        service = KnowledgeGateway()
        try:
            success = await service.add_document(
                doc_id=doc_id,
                title=title,
                content=raw_text,
                metadata={"category": "uploaded"},
            )
        except NotImplementedError as exc:
            # KnowledgeGateway.add_document / ingest_text 是**显式未实现**的桩。
            # 文本直接入库在当前版本不存在：检索域上传接口
            # （document_ingest.build_batch_upload_entries）同样只收 PDF。
            #
            # 因此这是**永久性**不支持，绝不能被下面的通用 except 吞成
            # FILE_INGEST_ERROR_MESSAGE（「请稍后再试」= 让用户永远重试）。
            # 这里显式分流，既保住准确文案，也把接线保留给将来实现。
            logger.warning(
                "text ingestion unavailable (KnowledgeGateway.add_document is a stub): "
                "suffix=%s doc_id=%s err=%s",
                suffix, doc_id, exc,
            )
            return {"messages": [AIMessage(
                content=FILE_TEXT_INGEST_UNAVAILABLE_MESSAGE.format(suffix=suffix)
            )]}
        if not success:
            return {"messages": [AIMessage(content="文件已读取，但保存到知识库失败，请稍后重试。")]}

        knowledge_node = create_knowledge_query_node(retrieval_port=get_retrieval_port())
        user_question = state.messages[-1].content if state.messages else title
        input_state: KnowledgeQueryInputState = {
            "task": user_question,
            "context": {"top_k": 5},
            "steps": ["file_upload"],
        }
        kb_result = await knowledge_node(input_state)
        answer_text = kb_result.get("answer") or f"文件《{title}》已上传并加入知识库，可直接对我提问相关内容。"
        return {"messages": [AIMessage(content=answer_text)]}
    except Exception as exc:
        logger.exception("Failed to ingest uploaded file: %s", exc)
        return {"messages": [AIMessage(content=FILE_INGEST_ERROR_MESSAGE)]}
