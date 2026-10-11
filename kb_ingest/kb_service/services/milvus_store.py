"""Milvus-backed vector store for the legal structured KB (R3.7).

替代原 pgvector（searchable_documents 表）后端：

- 集合固定为 `legal`（由 ``MILVUS_COLLECTION`` 覆盖）；
- 文档主键 = ``sha1(f"{source_table}|{source_id}")[:32]``，确定性可复算，
  因此 upsert / 增量检测 / 删除都能用主键精确命中；
- 嵌入统一做 L2 归一化后用 **IP** 度量 —— 数学上等价于 cosine
  （归一化向量内积即 cosine），与 agentix 主工程 R3.4 的口径一致；
- 结构化过滤（effective_on / authority / source_tables）走 Milvus
  JSON 字段表达式；company 模糊匹配 Milvus 不支持 ILIKE，
  采用「超量召回 k*3 + Python 子串后过滤」。
"""

import hashlib
import json
import logging
from typing import Dict, Iterable, List, Optional

from kb_service.clients.embedding import EmbeddingClient
from kb_service.core.config import Config
from kb_service.services.utils import compute_content_hash
from kb_service.services.legal_metadata import normalise_legal_metadata

COLLECTION_NAME = "legal"

# expr 内字符串转义（Milvus 表达式中的引号/反斜杠）
_EXPR_ESCAPE_CHARS = str.maketrans({"\\": "\\\\", '"': '\\"'})

# 增量检测时 expr 主键分批大小，避免表达式过长
_PK_QUERY_BATCH = 100


def _escape(value: str) -> str:
    """Escape a string for safe embedding into a Milvus boolean expression."""
    return str(value).translate(_EXPR_ESCAPE_CHARS)


def _doc_id(source_table: str, source_id: str) -> str:
    """Deterministic primary key for a (source_table, source_id) pair."""
    return hashlib.sha1(f"{source_table}|{source_id}".encode(), usedforsecurity=False).hexdigest()[:32]  # nosec B324


def _quoted(values: Iterable[str]) -> str:
    """Render values as a Milvus ``in ["a", "b"]`` list literal."""
    return ", ".join(f'"{_escape(str(v))}"' for v in values)


def _l2_normalize(vec: List[float]) -> List[float]:
    """L2-normalize a vector in place-free; zero vector is returned as-is."""
    norm = sum(x * x for x in vec) ** 0.5
    if norm <= 0:
        return list(vec)
    return [x / norm for x in vec]


class MilvusLegalStore:
    """Embedding generation + upsert/query/delete/search on the Milvus ``legal`` collection."""

    def __init__(self, config: Config, embedding_client: Optional[EmbeddingClient] = None):
        self.config = config
        self.embedding_client = embedding_client or EmbeddingClient(config)
        self.logger = logging.getLogger(__name__)
        self.collection_name = (
            getattr(config, "milvus_config", {}).get("collection") or COLLECTION_NAME
        )
        self._connected = False

    # ------------------------------------------------------------------ #
    # 连接与集合管理
    # ------------------------------------------------------------------ #
    def _connect(self):
        """Lazy connect + ensure collection/index exist; returns the Collection."""
        from pymilvus import (
            Collection,
            CollectionSchema,
            DataType,
            FieldSchema,
            connections,
            utility,
        )

        if not self._connected:
            connections.connect(
                alias="default",
                host=self.config.milvus_config["host"],
                port=self.config.milvus_config["port"],
            )
            self._connected = True

        if utility.has_collection(self.collection_name):
            return Collection(self.collection_name)

        dim = self._resolve_dimension()
        fields = [
            FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=64),
            FieldSchema(name="source_table", dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="source_id", dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=dim),
            FieldSchema(name="metadata", dtype=DataType.JSON),
            FieldSchema(name="content_hash", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(
                name="tenant_id",
                dtype=DataType.VARCHAR,
                max_length=64,
                partition_key=True,  # R3.4 口径：租户隔离走 partition key
            ),
        ]
        schema = CollectionSchema(fields=fields, description="Legal structured KB (kb_ingest)")
        collection = Collection(name=self.collection_name, schema=schema)
        collection.create_index(
            field_name="embedding",
            index_params={
                "metric_type": "IP",  # L2 归一化 + IP ≡ cosine
                "index_type": "IVF_FLAT",
                "params": {"nlist": 128},
            },
        )
        self.logger.info("已创建 Milvus 集合 %s（dim=%s, IP/IVF_FLAT）", self.collection_name, dim)
        return collection

    def _resolve_dimension(self) -> int:
        dim = int(getattr(self.embedding_client, "dimension", 0) or 0)
        if dim <= 0:
            probe_vec = self.embedding_client.embed_texts(["__probe__"])[0]
            dim = len(probe_vec)
            self.embedding_client.dimension = dim
        return dim

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def upsert(
        self,
        items: Iterable[dict],
        logger: Optional[logging.Logger] = None,
        incremental: bool = False,
    ) -> int:
        """Embed + upsert items into the Milvus collection.

        每个 item 至少包含 source_table / source_id / rewritten_content /
        original_data；签名与旧 pgvector VectorStoreWriter 保持一致。
        """
        items = list(items)
        if not items:
            (logger or self.logger).info("没有可嵌入的数据，跳过。")
            return 0

        log = logger or self.logger

        if incremental:
            items = self._filter_changed_items(items, log)
            if not items:
                log.info("增量模式：所有数据均未变化，跳过处理。")
                return 0
            log.info("增量模式：检测到 %s 条数据需要更新", len(items))

        vector_dim = self._resolve_dimension()
        collection = self._connect()

        batch_size = 32
        total = 0
        for start in range(0, len(items), batch_size):
            batch = items[start:start + batch_size]
            texts = [item.get("rewritten_content") or "" for item in batch]
            embeddings = self.embedding_client.embed_texts(texts)

            for idx, embedding in enumerate(embeddings):
                if len(embedding) != vector_dim:
                    raise ValueError(
                        f"第 {start + idx + 1} 条嵌入维度为 {len(embedding)}，与模型维度 {vector_dim} 不一致。"
                    )

            ids, tables, sids, contents, metas, hashes, tenants, vecs = [], [], [], [], [], [], [], []
            for item, embedding in zip(batch, embeddings):
                emb_list = self._to_float_list(embedding)
                meta = normalise_legal_metadata(item.get("original_data"))
                content_hash = compute_content_hash(item.get("original_data") or {})
                source_table = str(item.get("source_table") or "")
                source_id = str(item.get("source_id") if item.get("source_id") is not None else "")

                ids.append(_doc_id(source_table, source_id))
                tables.append(source_table)
                sids.append(source_id)
                contents.append(item.get("rewritten_content") or "")
                metas.append(meta)
                hashes.append(content_hash)
                tenants.append("kb_ingest")
                vecs.append(_l2_normalize(emb_list))

            # Milvus 无原生 upsert 语义的确定性冲突处理：先删后插（按主键）
            collection.delete(expr=f"id in [{_quoted(ids)}]")
            collection.insert(
                [ids, tables, sids, contents, vecs, metas, hashes, tenants]
            )
            total += len(batch)

        collection.flush()
        log.info("✓ 嵌入并入库完成，共处理 %s 条", total)
        return total

    def _filter_changed_items(self, items: List[dict], log: logging.Logger) -> List[dict]:
        """增量模式：按确定性主键批量取回 content_hash，过滤出变化的条目。"""
        if not items:
            return []

        collection = self._connect()
        pks = [
            _doc_id(
                str(item.get("source_table") or ""),
                str(item.get("source_id") if item.get("source_id") is not None else ""),
            )
            for item in items
        ]
        existing_hashes: Dict[str, str] = {}
        for start in range(0, len(pks), _PK_QUERY_BATCH):
            chunk = pks[start:start + _PK_QUERY_BATCH]
            expr = f"id in [{_quoted(chunk)}]"
            rows = collection.query(expr=expr, output_fields=["id", "content_hash"])
            for row in rows:
                existing_hashes[row["id"]] = row.get("content_hash") or ""

        log.info("从 Milvus 查询到 %s 条现有记录的哈希", len(existing_hashes))

        changed_items = []
        skipped_count = 0
        for item, pk in zip(items, pks):
            new_hash = compute_content_hash(item.get("original_data") or {})
            existing_hash = existing_hashes.get(pk)

            if existing_hash is None:
                changed_items.append(item)
                log.debug("新记录: %s/%s", item.get("source_table"), item.get("source_id"))
            elif existing_hash != new_hash:
                changed_items.append(item)
                log.debug(
                    "内容变化: %s/%s (旧哈希: %s, 新哈希: %s)",
                    item.get("source_table"), item.get("source_id"),
                    existing_hash[:8], new_hash[:8],
                )
            else:
                skipped_count += 1
                log.debug("跳过未变化: %s/%s", item.get("source_table"), item.get("source_id"))

        log.info("增量检测结果: 需更新 %s 条, 跳过 %s 条", len(changed_items), skipped_count)
        return changed_items

    # ------------------------------------------------------------------ #
    # 查询 / 删除（运维通道，供 processor.reprocess_by_filter / delete_records）
    # ------------------------------------------------------------------ #
    def query_records(
        self,
        source_table: Optional[str] = None,
        source_ids: Optional[List[str]] = None,
        company_name: Optional[str] = None,
        limit: int = 10000,
    ) -> List[dict]:
        """按条件取回记录（source_table / source_ids 精确匹配）。

        company_name 走 metadata 子串过滤（Milvus 无 ILIKE）。
        返回 dict：source_table / source_id / content / metadata(dict)。
        """
        collection = self._connect()

        conditions: List[str] = []
        if source_table:
            conditions.append(f'source_table == "{_escape(source_table)}"')
        if source_ids:
            conditions.append(f"source_id in [{_quoted(source_ids)}]")
        expr = " and ".join(conditions) if conditions else ""

        rows = collection.query(expr=expr or "", output_fields=["source_table", "source_id", "content", "metadata"], limit=limit)

        results = []
        for row in rows:
            meta = row.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:  # pragma: no cover - best effort
                    meta = {}
            if company_name:
                haystacks = (
                    str(meta.get("company_name") or ""),
                    str(meta.get("企业名称") or ""),
                )
                if not any(company_name in h for h in haystacks if h):
                    continue
            results.append(
                {
                    "source_table": row.get("source_table"),
                    "source_id": row.get("source_id"),
                    "content": row.get("content") or "",
                    "metadata": meta,
                }
            )
        return results

    def delete_records(self, source_table: str, source_ids: List[str]) -> int:
        """按 (source_table, source_id) 精确删除，返回受影响主键数。"""
        if not source_table or not source_ids:
            return 0
        collection = self._connect()
        pks = [_doc_id(str(source_table), str(sid)) for sid in source_ids]
        collection.delete(expr=f"id in [{_quoted(pks)}]")
        collection.flush()
        return len(pks)

    # ------------------------------------------------------------------ #
    # 向量检索
    # ------------------------------------------------------------------ #
    def search_similar(
        self,
        query_embedding: List[float],
        top_k: int = 5,
        threshold: Optional[float] = None,
        metric: str = "cosine",
        company_filter: Optional[str] = None,
        source_tables: Optional[List[str]] = None,
        effective_on=None,
        source_authority_level: Optional[str] = None,
    ) -> List[dict]:
        """ANN search on the ``legal`` collection.

        metric 仅接受 cosine / l2：集合统一使用 L2 归一化 + IP，
        归一化后 L2 距离单调等价于 cosine（||a-b||^2 = 2 - 2cos），
        因此 l2 请求同样按 IP 排序，distance 统一记为 1 - similarity。
        """
        if metric not in ("cosine", "l2"):
            raise ValueError(f"不支持的 metric: {metric}")

        collection = self._connect()

        expr = self._build_filter_expr(source_tables, effective_on, source_authority_level)
        qvec = _l2_normalize(self._to_float_list(query_embedding))

        # company 模糊匹配无法下推：超量召回 k*3 后 Python 子串过滤
        fetch_k = top_k * 3 if company_filter else top_k

        results = collection.search(
            data=[qvec],
            anns_field="embedding",
            param={"metric_type": "IP", "params": {"nprobe": 16}},
            limit=max(1, int(fetch_k)),
            expr=expr or None,
            output_fields=["source_table", "source_id", "content", "metadata"],
        )

        hits = results[0] if results else []
        items: List[dict] = []
        for hit in hits:
            entity = getattr(hit, "entity", None)
            row = entity._row_data if entity is not None and hasattr(entity, "_row_data") else {}

            meta = row.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:  # pragma: no cover - best effort
                    meta = {}

            similarity = float(hit.score)  # IP on normalized vectors == cosine
            distance = 1.0 - similarity

            if company_filter:
                haystacks = (
                    str(meta.get("company_name") or ""),
                    str(meta.get("企业名称") or ""),
                )
                if not any(company_filter in h for h in haystacks if h):
                    continue
            if threshold is not None and similarity < float(threshold):
                continue

            source_table = row.get("source_table")
            source_id = row.get("source_id")
            pk = getattr(hit, "id", "") or _doc_id(str(source_table or ""), str(source_id or ""))
            items.append(
                {
                    "id": str(pk),
                    "source_table": source_table,
                    "source_id": str(source_id) if source_id is not None else None,
                    "metadata": meta,
                    "content": row.get("content") or "",
                    "similarity": similarity,
                    "distance": distance,
                    "score": similarity,
                    "document_id": str(source_id) if source_id is not None else str(pk),
                    "source": meta.get("source")
                    or meta.get("url")
                    or meta.get("id")
                    or (str(source_id) if source_id is not None else str(pk)),
                }
            )

        items = items[:top_k]
        return items

    def _build_filter_expr(
        self,
        source_tables: Optional[List[str]],
        effective_on,
        source_authority_level: Optional[str],
    ) -> str:
        """构建 Milvus 布尔表达式（source_tables + effective_on + authority）。"""
        conditions: List[str] = []

        if source_tables:
            conditions.append(f"source_table in [{_quoted(source_tables)}]")

        if effective_on:
            effective = (
                effective_on.isoformat() if hasattr(effective_on, "isoformat") else str(effective_on)
            )
            eff = _escape(effective)
            key_from, key_to, _ = legal_metadata_keys()
            for key, op in ((key_from, "<="), (key_to, ">=")):
                k = f"metadata['{key}']"
                conditions.append(f'({k} == "" or {k} is null or {k} {op} "{eff}")')

        if source_authority_level:
            _, _, key_authority = legal_metadata_keys()
            auth = _escape(source_authority_level)
            conditions.append(f"metadata['{key_authority}'] == \"{auth}\"")

        return " and ".join(conditions)

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def _to_float_list(self, embedding) -> List[float]:
        if hasattr(embedding, "tolist"):
            embedding = embedding.tolist()
        if isinstance(embedding, (list, tuple)) and embedding and isinstance(embedding[0], (list, tuple)):
            embedding = [x for row in embedding for x in row]
        return [float(x) for x in embedding]


def legal_metadata_keys() -> tuple:
    """(effective_from, effective_to, authority_level) 的 canonical JSON key。"""
    from kb_service.services.legal_metadata import EFFECTIVE_FROM, EFFECTIVE_TO, AUTHORITY_LEVEL

    return (EFFECTIVE_FROM, EFFECTIVE_TO, AUTHORITY_LEVEL)
