
import re
from datetime import date
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field, field_validator

# 表名会被拼进 SELECT/SHOW SQL（反引号包裹），只接受纯标识符：
# 纯字母/数字/下划线，1-64 位——反引号、分号、空格、点号等一律拒绝。
_TABLE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]{1,64}$")


class ExcelIngestRequest(BaseModel):
    excel_path: str = Field(..., description="Path to the Excel file that needs processing.")
    regenerate: bool = Field(
        default=False,
        description="Force regeneration even if previous outputs exist.",
    )
    incremental: bool = Field(
        default=False,
        description="Enable incremental mode: only process changed rows based on content hash.",
    )


class MySQLIngestRequest(BaseModel):
    connection_url: str = Field(..., description="SQLAlchemy style MySQL connection URL.")
    table: str = Field(..., description="Table name to read from.")
    where: Optional[str] = Field(None, description="Optional SQL WHERE clause.")
    limit: Optional[int] = Field(None, ge=1, description="Maximum number of rows to ingest.")
    prompt_key: Optional[str] = Field(
        None, description="Optional prompt template key if not inferred from the table name."
    )
    extra_metadata: Dict[str, Any] = Field(default_factory=dict, description="Static metadata to persist.")
    legal_effective_from: Optional[date] = Field(None, description="法律知识生效日期。")
    legal_effective_to: Optional[date] = Field(None, description="法律知识失效日期（含当天）。")
    source_authority_level: Optional[str] = Field(None, description="来源权威等级。")
    effective_status: Optional[str] = Field(
        None,
        description="R2-2b 效力状态显式标注（in_force/pending/expired/repealed，中英别名可）；缺省由生效/失效日期推导。",
    )
    replaced_by: Optional[str] = Field(None, description="R2-2b 被何法替代（纯展示，随 metadata 落库）。")
    mode: Literal["rewrite", "flatten"] = Field(
        default="rewrite", description="Rewrite with LLM or directly flatten the row for embedding."
    )
    prompt_template: Optional[str] = Field(
        default=None,
        description="Override user prompt template (string.Template syntax).",
    )
    id_column: Optional[str] = Field(
        default=None,
        description="Column to use as unique identifier; defaults to row index if omitted.",
    )
    company_field: Optional[str] = Field(default=None, description="Field containing company name (optional).")
    report_year_field: Optional[str] = Field(default=None, description="Field containing report year (optional).")
    chunk_size: Optional[int] = Field(
        default=None,
        ge=1,
        description="Number of records to process per batch; defaults to processing all rows at once.",
    )

    @field_validator("table")
    @classmethod
    def _validate_table_identifier(cls, value: str) -> str:
        if not _TABLE_IDENTIFIER.match(value):
            raise ValueError(
                "table must be a plain SQL identifier (letters, digits, underscore; 1-64 chars)"
            )
        return value

    @field_validator("where")
    @classmethod
    def _validate_where_fragment(cls, value: Optional[str]) -> Optional[str]:
        """WHERE 片段由操作员提供（可信输入），仍拦截堆叠语句与注释截断。"""
        if value is None:
            return value
        if ";" in value:
            raise ValueError("where must not contain ';' (stacked statements)")
        if "--" in value or "/*" in value:
            raise ValueError("where must not contain SQL comments")
        return value
