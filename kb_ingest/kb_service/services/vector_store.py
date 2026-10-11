"""kb_ingest 向量写入兼容层（R3.7）。

pgvector（searchable_documents）后端已移除，`legal` 结构化库统一落在
Milvus（`MilvusLegalStore`）。保留 `VectorStoreWriter` 类名以兼容既有
调用方（processor / cli / entrypoint 链路），签名不变：

    writer.upsert(items, logger, incremental=False)
"""

import logging
from typing import Iterable, Optional

from kb_service.services.milvus_store import MilvusLegalStore


class VectorStoreWriter(MilvusLegalStore):
    """写入 Milvus ``legal`` 集合（原 pgvector 后端的兼容替身）。"""

    def upsert(
        self,
        items: Iterable[dict],
        logger: Optional[logging.Logger] = None,
        incremental: bool = False,
    ) -> int:
        return super().upsert(items, logger=logger, incremental=incremental)
