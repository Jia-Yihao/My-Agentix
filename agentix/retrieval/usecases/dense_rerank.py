"""Dense 单通道候选池的 Cross-Encoder 精排。

混合总闸关闭时检索链是 Dense-only，候选数等于 ``search_k``；精排要起作用，
需要先把候选池放大到 ``RETRIEVAL_RERANK_CANDIDATES``，重排后再截回 ``search_k``。
调用方负责放大候选数，本模块只负责重排与截断。

重排本身只有一条实现：``rerank_documents_concurrent`` 按
``RETRIEVAL_DENSE_RERANK_BATCH_SIZE`` 切连续批次、以
``RETRIEVAL_DENSE_RERANK_MAX_CONCURRENCY`` 为上限并发打上游，再全局融合；
超时统一读既有的 ``RERANKER_TIMEOUT_SECONDS``（单批 HTTP 与并发整体墙钟同一源），
不另开第二个超时键。批次大小 ≤0 表示单批覆盖全部候选，不再另走一条单次调用分支。

精排服务不可用、熔断打开、超时或返回异常时按原召回顺序截断（fail-open），
不做重排；成功时按重排后的顺序取前 ``search_k`` 条。
三类旁路事件分别记 ``rerank_breaker_total`` / ``rerank_failopen_total`` /
``rerank_skipped_total``（与 observability/metrics.py 同一套注册口径）。
"""

from __future__ import annotations

from agentix.retrieval.settings import settings


def _record(name: str) -> None:
    """观测记账失败绝不影响主链（约定 2）。"""
    try:
        from agentix.observability import metrics

        getattr(metrics, name)()
    except Exception:  # noqa: BLE001
        pass


async def rerank_dense_candidates(question: str, docs: list, search_k: int) -> list:
    """对放大的候选池重排并截到 ``search_k``；失败保留原序。"""
    if not docs or len(docs) <= search_k:
        _record("record_rerank_skipped")
        return list(docs[:search_k])

    try:
        from agentix.retrieval.infra.rag.rerank import rerank_documents_concurrent

        ranked, applied = await rerank_documents_concurrent(
            question,
            list(docs),
            batch_size=int(settings.RETRIEVAL_DENSE_RERANK_BATCH_SIZE),
            max_concurrency=int(settings.RETRIEVAL_DENSE_RERANK_MAX_CONCURRENCY),
            timeout=float(settings.RERANKER_TIMEOUT_SECONDS),
        )
    except Exception:  # noqa: BLE001 - 精排链路任何异常都按原召回顺序交付
        _record("record_rerank_failopen")
        return list(docs[:search_k])
    if not applied:
        _record("record_rerank_failopen")
        return list(docs[:search_k])
    return list(ranked[:search_k])
