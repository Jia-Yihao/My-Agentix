"""R-P1-1 Sparse 路：进程内 BM25（纯 Python，零新增依赖、零新服务）。

设计要点（检索增强深度优化.md §5 R-P1-1）：

- **语料复用文档入库链**：PostgreSQL ``chunk`` 表（活动产物通过
  ``document_repo.get_chunks_for_index_artifact`` 读取），
  与向量索引共用同一 ``chunk_id``（``Chunk.id`` ↔ index_worker 写入 dense 索引的
  ``metadata.chunk_id`` 同源）⇒ 两通道可按 chunk_id 做 z-score 去重融合。
- **索引快照一致**：生产检索按 ``artifact_id`` 缓存 BM25，并只索引该活动产物
  manifest 中已 ``ready`` 的切片，避免重建索引后 600 秒 TTL 内继续融合旧语料，
  或把尚未激活的新切片混入当前向量快照。
- **进程内缓存**：按 ``kb_id`` 缓存索引，TTL ``RETRIEVAL_SPARSE_CACHE_TTL`` 秒、
  容量上限 8、线程安全（DB 语料在锁外加载，双重建缓存无害）。
- **fail-open**：语料加载/检索任一异常 ⇒ warning + 返回 ``[]``，
  永不打断主链路（sparse 只是补充通道，dense 路照常出结果）。
- **倒排查询**：按 term posting 只遍历含查询词的文档，避免每次查询全量扫描 chunk；
  每篇文档的 TF 与长度只在索引构建期计算。
- **分词**：ASCII/数字词 + CJK 一元/二元切分（零依赖；词级中文分词属后续演进项）。
- **scope 一致**：候选一律经 ``chat_scope.meta_matches_filter`` 用与 FAISS 相同的
  filter 语义本地复算，file/folder 收窄不会被 sparse 路穿透。

分数方向：BM25 分越大越相关。
"""

from __future__ import annotations

import math
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from agentix.infrastructure.core.logger import get_logger
from agentix.retrieval.settings import settings

logger = get_logger(service="rag-sparse")

_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+")
_ARTICLE_RE = re.compile(r"第?\s*([0-9０-９零〇一二两三四五六七八九十百千万]+)\s*条")
_CN_NUMBER_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_NUMBER_UNITS = {"十": 10, "百": 100, "千": 1000}

_CACHE: Dict[Tuple[str, str | None], Tuple[float, "BM25Index"]] = {}
_CACHE_LOCK = threading.Lock()


def _cache_capacity() -> int:
    """缓存容量每次现读 settings：进程内改配置/测试 monkeypatch 都即时生效。

    模块级常量会把首次 import 时的值钉死——那一份值既不随配置变化，也让
    「调小容量」需要重启进程才能验证。
    """
    return max(1, int(settings.RETRIEVAL_SPARSE_CACHE_MAX_ENTRIES))


def _article_number(value: str) -> int | None:
    """将常见阿拉伯或中文数据条目号解析为整数；无法解析时不添加规范词项。"""
    normalized = "".join(str(int(char)) if char.isdecimal() else char for char in value)
    if len(normalized) > 12:
        return None
    if normalized.isdecimal():
        return int(normalized)
    if normalized and all(char in _CN_NUMBER_DIGITS for char in normalized):
        return int("".join(str(_CN_NUMBER_DIGITS[char]) for char in normalized))
    if not normalized or any(
        char not in _CN_NUMBER_DIGITS and char not in _CN_NUMBER_UNITS and char != "万"
        for char in normalized
    ):
        return None

    total = section = number = 0
    for char in normalized:
        if char in _CN_NUMBER_DIGITS:
            number = _CN_NUMBER_DIGITS[char]
        elif char == "万":
            section = (section + number) * 10_000
            total += section
            section = number = 0
        else:
            unit = _CN_NUMBER_UNITS[char]
            section += (number or 1) * unit
            number = 0
    return total + section + number


def tokenize(text: str) -> List[str]:
    """零依赖分词：ASCII/数字词、CJK 一元/二元及规范化数据条目号。

    CJK 用 unigram+bigram 是无词典场景下的标准近似：bigram 承载词组区分度，
    unigram 兜底单字命中；ASCII 词保留原词。同一数据条目的中/阿拉伯数字形式
    增加相同的 article:<number> 词项，避免编号格式差异导致词法通道漏召。
    """
    if not text:
        return []
    article_matches = list(_ARTICLE_RE.finditer(text))
    lexical_text = _ARTICLE_RE.sub(" ", text)
    tokens: List[str] = [m.group(0).lower() for m in _WORD_RE.finditer(lexical_text)]
    for run in _CJK_RUN_RE.findall(lexical_text):
        tokens.extend(run)
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    for match in article_matches:
        number = _article_number(match.group(1))
        if number is not None:
            tokens.append(f"article:{number}")
    return tokens


class BM25Index:
    """BM25 Okapi 的进程内倒排实现；k1/b 由 settings 注入，代码里不留形状参数。"""

    def __init__(
        self,
        records: Sequence[Tuple[str, str, Dict[str, Any]]],
        *,
        k1: Optional[float] = None,
        b: Optional[float] = None,
    ) -> None:
        # records: (doc_id, text, metadata)
        self.K1 = float(
            k1 if k1 is not None else settings.RETRIEVAL_SPARSE_BM25_K1
        )
        self.B = float(b if b is not None else settings.RETRIEVAL_SPARSE_BM25_B)
        self._ids: List[str] = []
        self._contents: List[str] = []
        self._metas: List[Dict[str, Any]] = []
        self._dl: List[int] = []
        self._by_id: Dict[str, int] = {}
        self._postings: Dict[str, List[Tuple[int, int]]] = {}
        for doc_id, text, meta in records:
            term_frequencies: Dict[str, int] = {}
            for term in tokenize(text or ""):
                term_frequencies[term] = term_frequencies.get(term, 0) + 1
            idx = len(self._ids)
            self._by_id.setdefault(doc_id, len(self._ids))
            self._ids.append(doc_id)
            self._contents.append(text or "")
            self._metas.append(dict(meta or {}))
            self._dl.append(sum(term_frequencies.values()))
            for term, frequency in term_frequencies.items():
                self._postings.setdefault(term, []).append((idx, frequency))
        n = len(self._ids)
        self._avgdl = (sum(self._dl) / n) if n else 0.0
        # idf 平滑（BM25 常用式）：恒 > 0，避免稀有词负分
        self._idf: Dict[str, float] = {
            term: math.log(1.0 + (n - len(postings) + 0.5) / (len(postings) + 0.5))
            for term, postings in self._postings.items()
        }

    def __len__(self) -> int:
        return len(self._ids)

    def search(self, query: str, top_n: int) -> List[Tuple[str, float]]:
        """返回 ``(doc_id, score)``，分数降序、并列按文档插入序（稳定）。"""
        if not self._ids or top_n <= 0:
            return []
        q_terms = set(tokenize(query))
        if not q_terms:
            return []
        avgdl = self._avgdl or 1.0
        scores: Dict[int, float] = {}
        for term in q_terms:
            idf = self._idf.get(term)
            if idf is None:
                continue
            for idx, frequency in self._postings.get(term, ()):
                dl = (self._dl[idx] / avgdl) if avgdl else 1.0
                norm = self.K1 * (1.0 - self.B + self.B * dl)
                scores[idx] = scores.get(idx, 0.0) + idf * (frequency * (self.K1 + 1.0)) / (
                    frequency + norm
                )
        ranked = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
        return [(self._ids[idx], score) for idx, score in ranked[:top_n] if score > 0.0]

    def meta_for(self, doc_id: str) -> Dict[str, Any]:
        idx = self._by_id.get(doc_id)
        return dict(self._metas[idx]) if idx is not None else {}

    def content_for(self, doc_id: str) -> str:
        idx = self._by_id.get(doc_id)
        return self._contents[idx] if idx is not None else ""


def _load_index(session, kb_id: str, ttl_seconds: int, artifact_id: str | None = None) -> BM25Index:
    """带 TTL 的进程内索引缓存（DB 语料在锁外加载；容量满时逐出最旧）。"""
    ttl = max(0, int(ttl_seconds))
    now = time.monotonic()
    cache_key = (kb_id, artifact_id)
    with _CACHE_LOCK:
        hit = _CACHE.get(cache_key)
        if hit is not None and (now - hit[0]) < ttl:
            return hit[1]

    from agentix.retrieval.infra.repos import document_repo

    if artifact_id is None:
        chunks = document_repo.get_chunks_by_kb(session, kb_id)
    else:
        chunks = document_repo.get_chunks_for_index_artifact(session, kb_id, artifact_id)
    records = [
        (
            chunk.id,
            chunk.content or "",
            {
                "chunk_id": chunk.id,
                "document_id": chunk.document_id,
                "page": chunk.page_no,
            },
        )
        for chunk in chunks
    ]
    index = BM25Index(
        records,
        k1=settings.RETRIEVAL_SPARSE_BM25_K1,
        b=settings.RETRIEVAL_SPARSE_BM25_B,
    )
    with _CACHE_LOCK:
        capacity = _cache_capacity()
        if len(_CACHE) >= capacity and cache_key not in _CACHE:
            oldest = min(_CACHE.items(), key=lambda kv: kv[1][0])
            _CACHE.pop(oldest[0], None)
        _CACHE[cache_key] = (time.monotonic(), index)
    logger.debug("BM25 index built for kb={} docs={}", kb_id, len(index))
    return index


def clear_cache() -> None:
    """清空索引缓存（测试/重建索引后主动失效用）。"""
    with _CACHE_LOCK:
        _CACHE.clear()


def sparse_search(
    *,
    session: Any,
    kb_id: str,
    query: str,
    top_n: int,
    metadata_filter: Optional[dict | Callable[[dict], bool]] = None,
    min_score: float = 0.0,
    ttl_seconds: int = 600,
    artifact_id: str | None = None,
) -> List[Tuple[Any, float]]:
    """BM25 检索：返回 ``(langchain Document, bm25_score)`` 列表（分数降序）。

    - scope 过滤用 ``meta_matches_filter``（与 FAISS 同语义）本地复算；
    - ``min_score > 0`` 时裁掉弱命中（默认 0.0 = 只保留 score>0 的命中）；
    - 任何异常 fail-open 返回 ``[]`` 并 warning（R-P1-1：sparse 是补充通道）。
    """
    try:
        if not query or not query.strip() or top_n <= 0:
            return []
        from agentix.retrieval.domain.services.chat_scope import meta_matches_filter

        index = _load_index(session, kb_id, ttl_seconds, artifact_id)
        # 过滤可能裁掉候选：按配置倍数多取再截断，保证过滤后仍能凑满 top_n
        multiplier = max(1, int(settings.RETRIEVAL_SPARSE_CANDIDATE_MULTIPLIER))
        hits = index.search(query, top_n=max(1, int(top_n) * multiplier))
        from langchain_core.documents import Document as LCDocument

        out: List[Tuple[Any, float]] = []
        for doc_id, score in hits:
            if score <= min_score:
                continue
            meta = index.meta_for(doc_id)
            if not meta_matches_filter(meta, metadata_filter):
                continue
            out.append(
                (
                    LCDocument(page_content=index.content_for(doc_id), metadata=meta),
                    float(score),
                )
            )
            if len(out) >= top_n:
                break
        return out
    except Exception as exc:  # noqa: BLE001 - fail-open：sparse 失败绝不打断主链路
        logger.warning("sparse BM25 search degraded (fail-open, kb={}): {}", kb_id, exc)
        return []
