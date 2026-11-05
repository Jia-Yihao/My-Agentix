"""Dense 单通道候选池的 Cross-Encoder 精排。

混合总闸关闭时检索链是 Dense-only，候选数等于 ``search_k``；精排要起作用，
需要先把候选池放大到 ``RETRIEVAL_RERANK_CANDIDATES``，重排后再截回 ``search_k``。
调用方负责放大候选数，本模块只负责重排与截断。

精排服务不可用、熔断打开或返回异常时按原召回顺序截断（fail-open），
不做重排；成功时按重排后的顺序取前 ``search_k`` 条。
"""

from __future__ import annotations


async def rerank_dense_candidates(question: str, docs: list, search_k: int) -> list:
    """对放大的候选池重排并截到 ``search_k``；失败保留原序。"""
    if not docs or len(docs) <= search_k:
        return list(docs[:search_k])

    try:
        from agentix.retrieval.infra.rag.rerank import rerank_documents_with_status

        ranked, applied = await rerank_documents_with_status(question, list(docs))
    except Exception:  # noqa: BLE001 - 精排链路任何异常都按原召回顺序交付
        return list(docs[:search_k])
    if not applied:
        return list(docs[:search_k])
    return list(ranked[:search_k])
