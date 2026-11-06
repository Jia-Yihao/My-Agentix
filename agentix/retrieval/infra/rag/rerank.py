"""主 RAG 链的可选精排客户端（默认关、全程 fail-open）。

协议自适应两种形态，端点由 ``RETRIEVAL_RERANK_BASE_URL`` +
``RETRIEVAL_RERANK_ENDPOINT`` 决定，默认回落 ``.env`` 里已有的 ``RERANK_*``：

- **DashScope text-rerank**（``.../services/rerank/text-rerank/text-rerank``）：
  ``POST {model, input:{query, documents}, parameters:{top_n, return_documents}}``
  → ``output.results: [{index, relevance_score}, ...]``；
- **通用自建服务**（含 ``scripts/local_rerank_server.py`` 的本地 CUDA 精排）：
  ``POST {query, documents, model, top_n}``
  → ``results: [{index, relevance_score|score}, ...]``。
  本地服务输出 ``sigmoid(logit) ∈ [0,1]``，与云端 relevance_score 同量纲，
  因此 ``RETRIEVAL_RERANK_MIN_SCORE`` 在两种端点间可直接互换。

任何网络/协议/解析异常都返回 ``None``，调用方必须原样保留融合顺序——
精排只能是「加分项」，绝不许把检索链打挂或打空（约定 2）。
分数口径统一为「越大越相关」，返回值按分数降序、并列按原始 index 升序。
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

import httpx

from agentix.infrastructure.core.logger import get_logger

logger = get_logger(service="rag-rerank")


def resolve_endpoint(base_url: str, endpoint: str) -> str:
    """拼出完整 URL；endpoint 已是完整 URL 时直接返回。空配置返回空串。"""
    ep = (endpoint or "").strip()
    base = (base_url or "").strip()
    if ep.startswith(("http://", "https://")):
        return ep
    if not base or not ep:
        return ""
    return f"{base.rstrip('/')}/{ep.lstrip('/')}"


def is_dashscope(endpoint: str) -> bool:
    """DashScope 官方 text-rerank 端点用 input/parameters 包裹的请求体。"""
    low = (endpoint or "").lower()
    return "dashscope" in low or "/services/rerank/" in low


def build_payload(
    *, query: str, texts: List[str], model: str, top_n: int, endpoint: str
) -> dict:
    n = max(1, int(top_n))
    if is_dashscope(endpoint):
        return {
            "model": model,
            "input": {"query": query, "documents": texts},
            "parameters": {"return_documents": False, "top_n": min(n, len(texts))},
        }
    return {
        "query": query,
        "documents": texts,
        "model": model,
        "top_n": min(n, len(texts)),
    }


def _extract_results(data: Any) -> Any:
    if not isinstance(data, dict):
        return None
    output = data.get("output")
    if isinstance(output, dict) and isinstance(output.get("results"), list):
        return output["results"]
    results = data.get("results")
    if isinstance(results, list):
        return results
    return None


def parse_results(data: Any, n_docs: int, top_n: int) -> Optional[List[Tuple[int, float]]]:
    """把响应解析为 ``[(原始 index, 分数)]``；无有效条目时返回 ``None``。"""
    results = _extract_results(data)
    if not results:
        return None
    out: List[Tuple[int, float]] = []
    seen = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        raw = item.get("relevance_score")
        if raw is None:
            raw = item.get("score")
        # bool 是 int 子类，显式排除；越界/重复/非数值条目一律跳过
        if isinstance(idx, bool) or not isinstance(idx, int):
            continue
        if not (0 <= idx < n_docs) or idx in seen:
            continue
        try:
            score = float(raw)
        except (TypeError, ValueError):
            continue
        seen.add(idx)
        out.append((idx, score))
    if not out:
        return None
    out.sort(key=lambda pair: (-pair[1], pair[0]))
    return out[: max(1, int(top_n))]


def rerank_candidates(
    *,
    query: str,
    texts: List[str],
    endpoint: str,
    api_key: str = "",
    model: str = "",
    top_n: int = 8,
    timeout_seconds: float = 3.0,
) -> Optional[List[Tuple[int, float]]]:
    """调用精排服务；失败/超时/空结果一律返回 ``None``（调用方保留原序）。

    这是**同步**函数：主链在事件循环里用 ``asyncio.to_thread`` 调用，
    避免一次外部 HTTP 阻塞整个检索链。
    """
    if not texts or not endpoint or not query:
        return None
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = build_payload(
        query=query, texts=texts, model=model, top_n=top_n, endpoint=endpoint
    )
    try:
        response = httpx.post(
            endpoint,
            json=payload,
            headers=headers,
            timeout=max(0.1, float(timeout_seconds)),
        )
        response.raise_for_status()
        paired = parse_results(response.json(), len(texts), top_n)
    except Exception as exc:  # noqa: BLE001 - 精排是可旁路增强，任何失败都 fail-open
        logger.warning("rerank degraded (fail-open): {}", exc)
        return None
    if paired is None:
        logger.warning("rerank returned no usable results (fail-open)")
    return paired
