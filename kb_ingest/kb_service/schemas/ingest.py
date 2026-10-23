
import re
from datetime import date
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 表名会被拼进 SELECT/SHOW SQL（反引号包裹），只接受纯标识符：
# 纯字母/数字/下划线，1-64 位——反引号、分号、空格、点号等一律拒绝。
_TABLE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]{1,64}$")


class MySQLFilterSpec(BaseModel):
    """⑫ 的结构化过滤条件：列名 + 封闭枚举运算符 + 一个值。

    这里**刻意不做校验**（列名形状、`in` 的值必须是列表、`is_null` 不该带值……），
    判据全部集中在 `core/sql_filters.build_where` 一处——校验散在两处就会不一致，
    而这条路径的错误消息必须由我们自己构造（不回显值，见 `db_url_guard.py` 同条口径）。

    `extra="forbid"`：多余键（比如有人试着塞 `sql`）必须报错而不是静默丢弃——
    静默丢弃会让调用方以为自己的条件生效了。
    """

    model_config = ConfigDict(extra="forbid")

    column: str = Field(..., description="Plain SQL identifier (letters, digits, underscore; 1-64 chars).")
    op: str = Field(
        ...,
        description=(
            "One of eq/ne/gt/ge/lt/le/like/in/not_in/is_null/is_not_null. "
            "Validated by core/sql_filters (closed enum), not by this schema."
        ),
    )
    value: Any = Field(
        None,
        description="Scalar for single-value ops; non-empty list for in/not_in; must be omitted for is_null/is_not_null.",
    )


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
    filters: Optional[List[MySQLFilterSpec]] = Field(
        None,
        description=(
            "Structured, parameter-bound filters. Only accepted when "
            "KB_MYSQL_WHERE_PARAMETRIZED=true; when that flag is on, `where` is rejected."
        ),
    )
    limit: Optional[int] = Field(None, ge=1, description="Maximum number of rows to ingest.")
    prompt_key: Optional[str] = Field(
        None, description="Optional prompt template key if not inferred from the table name."
    )
    extra_metadata: Dict[str, Any] = Field(default_factory=dict, description="Static metadata to persist.")
    legal_effective_from: Optional[date] = Field(None, description="测试数据知识生效日期。")
    legal_effective_to: Optional[date] = Field(None, description="测试数据知识失效日期（含当天）。")
    source_authority_level: Optional[str] = Field(None, description="来源权威等级。")
    effective_status: Optional[str] = Field(
        None,
        description="R2-2b 有效性状态显式标注（in_force/pending/expired/repealed，中英别名可）；缺省由生效/失效日期推导。",
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

    # ⚠️ **这里刻意不加 connection_url 的 field_validator**（G12 闸门放在
    # `MySQLIngestor.ingest` 与 `routes.ingest_mysql`，见 core/db_url_guard.py）。
    #
    # 原因不是"校验分层"这种口味问题，而是实测出来的凭据泄露：pydantic 的
    # ValidationError 会把**被拒的输入原样**塞进 `errors()["input"]`，FastAPI 的
    # 422 响应体又带着 `input` 字段。真跑一遍 `POST` 一条主机不在白名单内的 URL，
    # 响应里就是 `"input": "mysql://u:Tr3m0d1a@evil.example/kb"`——**密码明文回显**，
    # 而且只在错误路径上回显，正是调用方日志、网关访问日志、APM 最容易被留存的地方。
    # 校验放在服务层，异常由我们自己构造 detail，就能保证只说"为什么被拒"、不说"你传了什么"。

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
