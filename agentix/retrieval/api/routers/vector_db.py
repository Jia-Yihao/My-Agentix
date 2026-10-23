"""
向量库管理 API — 查看、分类、删除、上传索引内容

GET  /api/v1/vector-db/documents        — 列表（含分类标签、chunk 数、状态）
GET  /api/v1/vector-db/stats            — 统计信息
POST /api/v1/vector-db/classify         — 智能分类（结构检测 + LLM 标签）
DELETE /api/v1/vector-db/documents/{id} — 删除文档及其 chunk
POST /api/v1/vector-db/upload           — 直接上传文档入库
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Depends
from sqlmodel import Session

from agentix.retrieval.api.deps import get_current_user, DependsSession
from agentix.retrieval.domain.models.user import User
from agentix.retrieval.domain.models.document import Chunk
from agentix.retrieval.usecases.vector_db_service import (
    classify_documents,
    delete_document,
    get_stats,
    list_documents,
)

_logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_current_user)])


# ============================================================
# 智能分类
# ============================================================

# 文档内容关键词字典（用于 LLM 离线时的规则降级）
_CLASSIFY_KEYWORDS: dict[str, list[str]] = {
    "问答": ["问", "答", "什么是", "有哪些", "为什么", "如何", "怎么", "区别", "面试"],
    "条款协议": ["第", "条", "章", "甲方", "乙方", "协议", "责任", "义务"],
    "技术文档": ["API", "接口", "配置", "部署", "Python", "代码", "函数", "class"],
    "财务报告": ["营收", "利润", "成本", "毛利", "现金流", "资产", "负债", "财报"],
    "学术论文": ["摘要", "引言", "方法", "实验", "结果", "结论", "参考", "文献"],
    "通用": [],
}


def _classify_document(chunks: list[Chunk]) -> dict[str, Any]:
    """智能分类：先走结构检测，再补充 LLM 标签，降级规则兜底。"""
    if not chunks:
        return {"category": "空文档", "tags": [], "confidence": 0}

    all_text = " ".join(c.text for c in chunks if c.text)
    if not all_text:
        return {"category": "空文档", "tags": [], "confidence": 0}

    # 1) 结构检测
    strategy = _detect_strategy(all_text)

    # 2) LLM 标签（如果可用）
    tags = []
    try:
        tags = _llm_tag_document(all_text[:2000])
    except Exception:
        tags = []

    # 3) 降级：规则匹配
    if not tags:
        tags = _rule_tag_document(all_text)

    return {
        "category": _strategy_to_category(strategy),
        "tags": tags,
        "strategy_detected": strategy,
        "chunk_count": len(chunks),
        "total_chars": len(all_text),
    }


def _detect_strategy(text: str) -> str:
    """用文本信号检测文档类型。"""
    lines = text.splitlines()
    q_count = sum(1 for line in lines if re.match(r"^\s*(\d{1,3})\s*[\.、]", line))
    clause_count = sum(1 for line in lines if re.match(r"^(第[一二三四五六七八九十百\d]+[章节条])", line))
    heading_count = sum(1 for line in lines if re.match(r"^(摘要|Abstract|引言|结论|参考)", line, re.I))

    if q_count >= 3:
        return "qa_image"
    if clause_count >= 3:
        return "clause"
    if heading_count >= 2:
        return "article"
    return "article"


def _strategy_to_category(strategy: str) -> str:
    return {
        "qa_image": "问答资料",
        "article": "文章/论文",
        "clause": "条款/协议文档",
        "visual": "图表/扫描件",
        "mineru_structured": "MinerU 解析",
    }.get(strategy, strategy)


def _llm_tag_document(text: str) -> list[str]:
    """调用主应用 LLM 给文档打标签；失败时由规则分类兜底。"""
    from agentix.retrieval.usecases.chat_llm import create_chat_llm

    prompt = """分析以下文档内容，提取 3~5 个标签（逗号分隔），用于分类检索。
标签应简短（2-5 字），如"招聘"、"协议"、"技术方案"、"财报"等。
只输出标签，不要解释。

文档内容：
"""
    try:
        llm = create_chat_llm(temperature=0.1)
        resp = llm.invoke(prompt + text[:1500])
        response_text = str(getattr(resp, "content", "") or "")
    except Exception as exc:
        _logger.warning("document tag LLM failed: %s", exc)
        return []
    tags = [t.strip() for t in response_text.replace("\n", ",").split(",") if t.strip()]
    return tags[:5]


def _rule_tag_document(text: str) -> list[str]:
    """规则兜底：关键词匹配提取标签。"""
    lowered = text.lower()
    tags = []
    for category, kws in _CLASSIFY_KEYWORDS.items():
        if category == "通用":
            continue
        score = sum(1 for kw in kws if kw.lower() in lowered)
        if score >= 2:
            tags.append(category)
    return tags[:5]


# ============================================================
# API 端点
# ============================================================

@router.get("/vector-db/stats")
def get_vector_db_stats(
    session: Session = DependsSession,
    _: User = Depends(get_current_user),
) -> dict:
    """获取向量库统计信息。"""
    stats = get_stats(session)
    # 按分类统计
    return {
        "data": {
            "total_documents": stats["total_documents"],
            "total_chunks": stats["total_chunks"],
            "collections": 1,
            "backend": "local",
        }
    }


@router.get("/vector-db/documents")
def list_vector_db_documents(
    session: Session = DependsSession,
    _: User = Depends(get_current_user),
    search: str = "",
    category: str = "",
    kb_id: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """列出向量库中的文档（含分类标签）。"""
    result, total = list_documents(
        session,
        search=search,
        category=category,
        kb_id=kb_id,
        page=page,
        page_size=page_size,
        classify=_classify_document,
    )

    return {"data": result, "total": total, "page": page, "page_size": page_size}


@router.delete("/vector-db/documents/{document_id}")
def delete_vector_db_document(
    document_id: str,
    session: Session = DependsSession,
    _: User = Depends(get_current_user),
) -> dict:
    """从向量库中删除文档及其 chunk。"""
    try:
        delete_document(session, document_id)
        return {"data": {"deleted": document_id}}
    except Exception as exc:
        _logger.exception("删除文档失败")
        return {"error": str(exc)}


@router.post("/vector-db/classify")
def classify_vector_db_documents(
    document_ids: list[str] | None = None,
    session: Session = DependsSession,
    _: User = Depends(get_current_user),
) -> dict:
    """对指定文档进行智能分类，返回分类结果。"""
    results = classify_documents(session, document_ids or [], _classify_document)
    return {"data": results}
