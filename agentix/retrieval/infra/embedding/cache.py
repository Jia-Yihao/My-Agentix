"""
Embedding 结果本地缓存（stdlib sqlite3，无新增依赖）

动机
----
``_build_vector_index`` 会取出该 KB 的**全部** chunk 交给 FAISS 重建索引，因此即使只改动
一个文档，未变动文档的 chunk 也会被重新嵌入；而「chunk 文本 → 向量」是确定性映射，
重复嵌入纯属重复计费与时延。缓存按内容哈希复用历史结果。

设计要点
--------
- **键 = sha256(model + "\\x00" + text)**：换嵌入模型自然失效，绝不会串向量。
- **批内先按内容去重**：同一文本在批内出现多次只请求一次（重复内容只计费一次）。
- **部分命中只请求未命中项**：命中项直接复用，未命中项合并成一次批量请求。
- **FIFO 上限裁剪**：避免缓存文件无限增长（按写入时间淘汰最旧的）。
- **纯派生数据**：删除缓存文件只会退回「无缓存」行为，不影响正确性。
- 落盘为 float32（本机字节序）；跨机器拷贝缓存不保证可用，直接删掉即可。

返回值顺序与输入严格一一对应（含重复项），不改变调用方既有语义。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sqlite3
import threading
import time
from array import array
from typing import Sequence

from langchain_core.embeddings import Embeddings

logger = logging.getLogger(__name__)

DEFAULT_MAX_ENTRIES = 200_000
# sqlite 单条语句的变量数有上限，命中查询按此分片
_SELECT_CHUNK = 500
# 触发裁剪后回落到上限的该比例
_PRUNE_TARGET_RATIO = 0.8


def embedding_cache_key(text: str, model: str) -> str:
    """内容哈希缓存键：把模型名并入前缀，换模型即整体失效。"""
    return hashlib.sha256(f"{model}\x00{text}".encode()).hexdigest()


def _to_blob(vector: Sequence[float]) -> bytes:
    return array("f", (float(x) for x in vector)).tobytes()


def _from_blob(blob: bytes) -> list[float]:
    arr = array("f")
    arr.frombytes(blob)
    return list(arr)


class EmbeddingCache:
    """内容哈希 → 向量 的本地 sqlite 缓存（线程安全）。"""

    def __init__(self, path: str, *, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self._path = path
        self._max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS embeddings ("
            " key TEXT PRIMARY KEY,"
            " model TEXT NOT NULL,"
            " dim INTEGER NOT NULL,"
            " vector BLOB NOT NULL,"
            " created_at REAL NOT NULL)"
        )
        self._conn.commit()
        # 行数上界估计：每次写入按条数累加（INSERT OR REPLACE 可能不新增行 → 只会高估，
        # 高估只会让裁剪更早发生，方向安全），避免每批都做一次 COUNT(*)。
        self._known_count = self._count_locked()

    @property
    def path(self) -> str:
        return self._path

    def get_many(self, keys: list[str]) -> dict[str, list[float]]:
        """批量取向量，只返回命中的项。"""
        if not keys:
            return {}
        found: dict[str, list[float]] = {}
        with self._lock:
            for i in range(0, len(keys), _SELECT_CHUNK):
                batch = keys[i : i + _SELECT_CHUNK]
                placeholders = ",".join("?" * len(batch))
                rows = self._conn.execute(
                    f"SELECT key, vector FROM embeddings WHERE key IN ({placeholders})",  # nosec B608 - 表名常量，IN 占位符全部参数绑定
                    batch,
                ).fetchall()
                for key, blob in rows:
                    found[key] = _from_blob(blob)
        return found

    def put_many(self, items: dict[str, list[float]], *, model: str) -> None:
        """批量写入向量（同键覆盖），并在超出上限时裁剪。"""
        if not items:
            return
        now = time.time()
        rows = [(key, model, len(vec), _to_blob(vec), now) for key, vec in items.items()]
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO embeddings (key, model, dim, vector, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()
            self._known_count += len(rows)
            if self._known_count > self._max_entries:
                self._prune_locked()

    def _count_locked(self) -> int:
        (count,) = self._conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()
        return int(count)

    def _prune_locked(self) -> None:
        """超过上限时按写入时间淘汰最旧的一批（FIFO），保证条数收敛到上限以内。"""
        count = self._count_locked()
        if count <= self._max_entries:
            self._known_count = count
            return
        target = max(1, int(self._max_entries * _PRUNE_TARGET_RATIO))
        excess = count - target
        self._conn.execute(
            "DELETE FROM embeddings WHERE key IN ("
            " SELECT key FROM embeddings ORDER BY created_at ASC LIMIT ?)",
            (excess,),
        )
        self._conn.commit()
        self._known_count = target
        logger.info(
            "embedding cache pruned: removed=%s remaining=%s path=%s",
            excess,
            target,
            self._path,
        )

    def count(self) -> int:
        with self._lock:
            return self._count_locked()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class CachedEmbeddings(Embeddings):
    """给任意 ``Embeddings`` 实例套一层内容哈希缓存。"""

    def __init__(self, inner: Embeddings, cache: EmbeddingCache, *, model: str) -> None:
        self._inner = inner
        self._cache = cache
        self._model = model

    def __getattr__(self, name: str):
        # 仅当自身无该属性时委托给被包装实例（模型名、client 等仍可访问）
        return getattr(object.__getattribute__(self, "_inner"), name)

    def _safe_get(self, keys: list[str]) -> dict[str, list[float]]:
        """缓存读取失败不得影响主流程，退化为「全部未命中」。"""
        try:
            return self._cache.get_many(keys)
        except Exception:
            logger.warning("embedding cache read failed; treating as cache miss", exc_info=True)
            return {}

    def _safe_put(self, items: dict[str, list[float]]) -> None:
        """缓存写入失败不得影响主流程（下次仍会重新嵌入）。"""
        try:
            self._cache.put_many(items, model=self._model)
        except Exception:
            logger.warning("embedding cache write failed; result not cached", exc_info=True)

    def cached_count_for(self, texts: Sequence[str]) -> int:
        """这些文本里**已有缓存**的条数（按键去重后计）。只读：不发网络请求、不写缓存。

        存在的理由是 E4 预热要报告"刷了几条、其中几条本来就命中"，而键计算
        （``embedding_cache_key``）与模型名是本类的内部事实——让调用方自己算键等于给
        同一套键逻辑造第二个源，换模型/改键格式时那份会静默对上号不上。
        读取失败按 0 命中处理（与 ``_safe_get`` 同口径：缓存是加速件，不是正确性件）。
        """
        if not texts:
            return 0
        keys = list(dict.fromkeys(embedding_cache_key(text, self._model) for text in texts))
        try:
            return len(self._cache.get_many(keys))
        except Exception:
            logger.warning("embedding cache probe failed; treating as empty", exc_info=True)
            return 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        keys = [embedding_cache_key(text, self._model) for text in texts]
        cached = self._safe_get(keys)

        # 企业化 P1-5.2：embedding 缓存命中率打点（条目级，失败不影响主流程）
        try:
            from agentix.observability.metrics import record_cache_hit, record_cache_miss

            hits = sum(1 for k in keys if k in cached)
            if hits:
                record_cache_hit("embedding")
            if len(keys) - hits:
                record_cache_miss("embedding")
        except Exception:  # noqa: BLE001
            pass

        # 批内去重：同一内容只请求一次
        missing: dict[str, str] = {}
        for text, key in zip(texts, keys):
            if key not in cached and key not in missing:
                missing[key] = text

        if missing:
            miss_keys = list(missing)
            vectors = self._inner.embed_documents([missing[k] for k in miss_keys])
            if len(vectors) != len(miss_keys):
                raise ValueError(
                    f"embedding 返回条数与请求不符: got={len(vectors)}, expected={len(miss_keys)}"
                )
            self._safe_put(dict(zip(miss_keys, vectors)))
            cached.update(dict(zip(miss_keys, vectors)))

        return [cached[key] for key in keys]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        # 无原生异步实现时复用同步路径（含缓存），并移出事件循环
        return await asyncio.to_thread(self.embed_documents, texts)

    async def aembed_query(self, text: str) -> list[float]:
        return await asyncio.to_thread(self.embed_query, text)


def resolve_cache_path() -> str:
    """缓存文件位置：显式配置优先，否则放 DATA_DIR（每个环境相互隔离）。"""
    from agentix.retrieval.settings import settings

    configured = (getattr(settings, "EMBEDDING_CACHE_PATH", "") or "").strip()
    if configured:
        return configured
    return os.path.join(str(settings.DATA_DIR), "embedding_cache.db")


def build_embedding_cache() -> EmbeddingCache | None:
    """按配置创建缓存实例；关闭或初始化失败时返回 None（调用方退回无缓存）。"""
    from agentix.retrieval.settings import settings

    if not getattr(settings, "EMBEDDING_CACHE_ENABLED", False):
        return None
    try:
        return EmbeddingCache(
            resolve_cache_path(),
            max_entries=getattr(settings, "EMBEDDING_CACHE_MAX_ENTRIES", DEFAULT_MAX_ENTRIES),
        )
    except Exception:
        logger.warning("embedding cache init failed; falling back to uncached", exc_info=True)
        return None


def with_embedding_cache(embeddings: Embeddings, *, model: str) -> Embeddings:
    """按配置给 embeddings 套上缓存；不可用时原样返回。"""
    cache = build_embedding_cache()
    if cache is None:
        return embeddings
    return CachedEmbeddings(embeddings, cache, model=model)
