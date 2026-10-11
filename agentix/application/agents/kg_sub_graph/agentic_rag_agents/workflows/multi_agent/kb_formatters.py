"""KB 检索结果的格式化纯函数（P1-1 拆分）。

从 multi_tool.py 搬移并参数化：top_k 不再来自闭包，全部显式传参，
保证零依赖可单测（方案[第5章]：格式化函数一律为纯函数）。
"""

from typing import Any, Dict, List

# 工具标签：用于检索结果片段的来源标注
TOOL_LABELS = {
    "milvus": "Milvus",
    "postgres": "PostgreSQL",
}


def history_to_text(history: List[Dict[str, str]], limit: int = 4) -> str:
    if not history:
        return "（无历史对话）"
    segments: List[str] = []
    for item in history[-limit:]:
        role = item.get("role", "user")
        content = item.get("content", "")
        segments.append(f"{role}: {content}")
    return "\n".join(segments)


def format_results(
    results: List[Dict[str, Any]],
    *,
    top_k: int,
    default_label: str,
    empty_hint: str,
) -> str:
    if not results:
        return empty_hint
    snippets: List[str] = []
    for idx, doc in enumerate(results[:top_k]):
        content = doc.get("content") or doc.get("document") or ""
        snippet = content.strip().replace("\n", " ")
        snippet = snippet[:500]
        meta = doc.get("metadata") or {}
        source = (
            doc.get("source")
            or doc.get("source_table")
            or meta.get("source")
            or meta.get("source_table")
            or meta.get("title")
            or ""
        )
        tool_label = TOOL_LABELS.get(str(doc.get("tool", "")).lower(), default_label)
        tag = f"[{tool_label}#{idx + 1}] {snippet}"
        if source:
            tag = f"{tag}\n来源：{source}"
        snippets.append(tag)
    return "\n\n".join(snippets)


def format_milvus_results(results: List[Dict[str, Any]], *, top_k: int) -> str:
    return format_results(
        results,
        top_k=top_k,
        default_label="Milvus",
        empty_hint="（Milvus 暂无检索结果）",
    )


def format_postgres_results(results: List[Dict[str, Any]], *, top_k: int) -> str:
    return format_results(
        results,
        top_k=top_k,
        default_label="PostgreSQL",
        empty_hint="（PostgreSQL 暂无检索结果）",
    )


def format_combined_local_results(results: List[Dict[str, Any]], *, top_k: int) -> str:
    return format_results(
        results,
        top_k=top_k,
        default_label="本地",
        empty_hint="（无本地检索结果）",
    )


def format_external_results(results: List[Dict[str, Any]], *, top_k: int) -> str:
    if not results:
        return "（无外部检索结果）"
    snippets: List[str] = []
    for idx, item in enumerate(results[:top_k]):
        content = item.get("content") or item.get("summary") or ""
        snippet = content.strip().replace("\n", " ")
        snippet = snippet[:500]
        meta = item.get("metadata") or {}
        source = (
            item.get("source")
            or item.get("source_table")
            or meta.get("source")
            or meta.get("source_table")
            or meta.get("url")
            or ""
        )
        tag = f"[外部#{idx + 1}] {snippet}"
        if source:
            tag = f"{tag}\n来源：{source}"
        snippets.append(tag)
    return "\n\n".join(snippets)


def collect_sources(
    *result_sets: List[Dict[str, Any]],
) -> List[str]:
    collected: List[str] = []
    for dataset in result_sets:
        for doc in dataset or []:
            meta = doc.get("metadata") or {}
            candidate = (
                doc.get("source")
                or doc.get("source_table")
                or doc.get("document_id")
                or doc.get("source_id")
                or doc.get("id")
                or meta.get("source")
                or meta.get("source_table")
                or meta.get("url")
                or meta.get("title")
            )
            if candidate:
                collected.append(str(candidate))
    # 去重但保留顺序
    seen: Dict[str, None] = {}
    for source in collected:
        seen.setdefault(source, None)
    return list(seen.keys())
