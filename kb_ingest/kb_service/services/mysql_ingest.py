
import logging
import time
from typing import Any, Dict, List, Optional

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import create_engine

from kb_service.clients.llm import LLMClient
from kb_service.core.config import Config
from kb_service.core.db_url_guard import enforce_connection_url
from kb_service.core.sql_filters import build_where, enforce_where_policy
from kb_service.prompts.manager import PromptManager, SchemaColumn, build_prompt_manager_from_env
from kb_service.schemas.ingest import MySQLIngestRequest
from kb_service.services.utils import flatten_row
from kb_service.services.vector_store import VectorStoreWriter
from kb_service.services.legal_metadata import normalise_legal_metadata


class MySQLIngestor:
    """Ingests MySQL rows, rewrites or flattens them, and writes embeddings."""

    def __init__(self, config: Config, prompt_manager: Optional[PromptManager] = None):
        self.config = config
        self.prompt_manager = prompt_manager or build_prompt_manager_from_env()
        self.llm_client = LLMClient(config)
        self.vector_writer = VectorStoreWriter(config)
        self.logger = logging.getLogger(__name__)

    def ingest(self, request: MySQLIngestRequest) -> Dict[str, int]:
        # G12：连接串闸门（默认关）。放在这里是因为它是 HTTP 与 CLI 的**唯一**汇合点，
        # 放在 pydantic 层则会让被拒的连接串连密码一起进 422 响应体（见 schemas 侧注释）。
        enforce_connection_url(request.connection_url, self.config)
        # ⑫：WHERE 口径同样在**建连接之前**判掉——参数化的拒绝理由全在本地可判定，
        # 等到 `_build_query` 才发现，调用方付出的是一次真实的数据库握手。
        enforce_where_policy(
            self.config.mysql_where_parametrized, request.where, request.filters
        )
        engine = create_engine(request.connection_url)
        total_rows = 0
        embedded_rows = 0

        with engine.connect() as conn:
            schema = self._fetch_schema(conn, request.table)
            query = self._build_query(request)

            chunks = pd.read_sql_query(query, conn, chunksize=request.chunk_size)
            if isinstance(chunks, pd.DataFrame):
                embedded_rows += self._process_chunk(chunks, schema, request)
                total_rows += len(chunks)
            else:
                for chunk in chunks:
                    total_rows += len(chunk)
                    embedded_rows += self._process_chunk(chunk, schema, request)

        self.logger.info("MySQL ingest完成: 共读取 %s 条, 写入 %s 条", total_rows, embedded_rows)
        return {"rows_read": total_rows, "rows_embedded": embedded_rows}

    def _process_chunk(
        self,
        chunk: pd.DataFrame,
        schema: List[SchemaColumn],
        request: MySQLIngestRequest,
    ) -> int:
        if chunk.empty:
            return 0

        items: List[Dict] = []
        template_key = request.prompt_key or request.table
        override_template = request.prompt_template
        effective_schema = schema or [SchemaColumn(name=str(col)) for col in chunk.columns]

        for idx, row in chunk.iterrows():
            row_dict = {k: (None if pd.isna(v) else v) for k, v in row.to_dict().items()}
            merged_meta = {**row_dict, **request.extra_metadata}
            legal_fields = {
                "legal_effective_from": request.legal_effective_from,
                "legal_effective_to": request.legal_effective_to,
                "source_authority_level": request.source_authority_level,
                # R2-2b（指南 §2.2.7①）：四字段有效性契约——显式状态与替代法透传（人工标注已失效入口）
                "effective_status": request.effective_status,
                "replaced_by": request.replaced_by,
            }
            merged_meta = normalise_legal_metadata(
                {**merged_meta, **{k: v for k, v in legal_fields.items() if v is not None}}
            )

            source_id = self._extract_identifier(row_dict, request.id_column, idx)
            company_name = self._extract_optional_field(row_dict, request.company_field)
            report_year = self._extract_optional_field(row_dict, request.report_year_field)

            if request.mode == "flatten":
                content = flatten_row(merged_meta, self.config)
            else:
                system_prompt, user_prompt = self.prompt_manager.get_prompt(
                    template_key,
                    merged_meta,
                    schema=effective_schema,
                    override_template=override_template,
                    context_table_name=request.table,
                )
                content = self._generate_with_retry(user_prompt, system_prompt)
                if not content:
                    self.logger.warning("跳过第 %s 行，LLM 未返回内容", idx)
                    continue

            items.append(
                {
                    "source_table": request.table,
                    "source_id": source_id,
                    "rewritten_content": content,
                    "company_name": company_name or "",
                    "report_year": report_year or "",
                    "original_data": merged_meta,
                }
            )

        if not items:
            return 0

        return self.vector_writer.upsert(items, self.logger)

    def _generate_with_retry(self, prompt: str, system_prompt: str) -> Optional[str]:
        for attempt in range(self.config.retry_times):
            try:
                content = self.llm_client.generate(prompt, system_prompt)
                if content:
                    return content
            except Exception as exc:
                self.logger.warning("LLM 生成失败 (尝试 %s/%s): %s", attempt + 1, self.config.retry_times, exc)
                if attempt < self.config.retry_times - 1:
                    time.sleep(self.config.retry_delay)
        return None

    def _extract_identifier(self, row: Dict, id_column: Optional[str], fallback_idx: int) -> str:
        if id_column and id_column in row and row[id_column] is not None:
            return str(row[id_column])
        return str(fallback_idx)

    def _extract_optional_field(self, row: Dict, field_name: Optional[str]) -> Optional[str]:
        if not field_name:
            return None
        value = row.get(field_name)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _build_query(self, request: MySQLIngestRequest):
        parametrized = self.config.mysql_where_parametrized
        enforce_where_policy(parametrized, request.where, request.filters)
        base = f"SELECT * FROM `{request.table}`"  # nosec B608 - 表名已由 MySQLIngestRequest 标识符白名单校验
        params: Dict[str, Any] = {}
        if parametrized:
            # flag 开：原始 `where` 已在上面被拒，这里只可能来自封闭枚举 + bindparams。
            if request.filters:
                clause, params = build_where([f.model_dump() for f in request.filters])
                base += f" WHERE {clause}"
        elif request.where:
            # flag 关：与加固前逐位等价（`where` 已拒绝 ;/-- 注释，仍是拼接片段）。
            base += f" WHERE {request.where}"
        if request.limit:
            base += f" LIMIT {int(request.limit)}"
        statement = text(base)
        if params:
            statement = statement.bindparams(**params)
        return statement

    def _fetch_schema(self, conn, table: str) -> List[SchemaColumn]:
        result = conn.execute(text(f"SHOW FULL COLUMNS FROM `{table}`"))
        schema = []
        for row in result:
            mapping = row._mapping if hasattr(row, "_mapping") else row
            schema.append(
                SchemaColumn(
                    name=mapping.get("Field"),
                    data_type=mapping.get("Type"),
                    comment=mapping.get("Comment"),
                )
            )
        return schema
