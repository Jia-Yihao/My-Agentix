
import logging
from pathlib import Path
from typing import Iterable, Sequence

import regex as re
from langchain_neo4j import Neo4jGraph

from agentix.config import settings

from .regex_patterns import get_cypher_query_node_graph_schema

logger = logging.getLogger(__name__)

COL_CHINESE_MEANING = "\u4e2d\u6587\u542b\u4e49"
COL_MAIN_PROPERTIES = "\u4e3b\u8981\u5c5e\u6027"
COL_NOTE_EXAMPLE = "\u5907\u6ce8\u793a\u4f8b"
COL_REL_TYPE = "\u7c7b\u578b"
COL_REL_DIRECTION = "\u8d77\u70b9 \u2192 \u7ec8\u70b9"
ALT_REL_DIRECTION = "\u8d77\u70b9 -> \u7ec8\u70b9"
SECTION_ATTRIBUTES = "\u8282\u70b9\u4e0e\u5173\u7cfb\u5c5e\u6027"


def _extract_table_blocks(markdown: str) -> list[list[str]]:
    """
    Extract consecutive Markdown table blocks (lines starting with '|').
    """
    tables: list[list[str]] = []
    current_block: list[str] = []

    for line in markdown.splitlines():
        if line.strip().startswith("|"):
            current_block.append(line)
        elif current_block:
            tables.append(current_block)
            current_block = []

    if current_block:
        tables.append(current_block)

    return tables


def _parse_markdown_table(lines: Iterable[str]) -> tuple[list[str], list[list[str]]]:
    """
    Parse a Markdown table block into a header row and table body.
    """
    header: list[str] | None = None
    rows: list[list[str]] = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        cells = [cell.strip().strip("`") for cell in line.strip("|").split("|")]

        if header is None:
            header = cells
            continue

        # Skip separator rows like | --- | --- |
        if all(not cell or set(cell) <= {"-", ":", " "} for cell in cells):
            continue

        rows.append(cells)

    return (header or []), rows


def _extract_markdown_section(markdown: str, heading: str) -> str:
    """
    Extract the content of a Markdown section by its '## ' heading.
    """
    pattern = rf"^##\s*{re.escape(heading)}\s*$([\s\S]*?)(?=^##\s|\Z)"
    match = re.search(pattern, markdown, flags=re.MULTILINE)
    if not match:
        return ""

    section_text = match.group(1).strip()
    cleaned_lines = [
        line.rstrip()
        for line in section_text.splitlines()
        if line.strip() and line.strip() != "** End Patch"
    ]

    return "\n".join(cleaned_lines).strip()


def _clean_cell(value: str) -> str:
    """归一化表格单元格：去掉包裹/内联的反引号与多余空白。"""
    return value.replace("`", "").strip()


def _lookup_cell(
    cells: Sequence[str],
    header: Sequence[str],
    possible_keys: Sequence[str],
    fallback_index: int | None = None,
) -> str:
    """
    Fetch a cell value by column name, with optional positional fallback.
    """
    for key in possible_keys:
        if key in header:
            idx = header.index(key)
            if idx < len(cells):
                return _clean_cell(cells[idx])
    if fallback_index is not None and fallback_index < len(cells):
        return _clean_cell(cells[fallback_index])
    return ""


def _format_legal_schema_from_docs() -> str:
    """
    Build a concise, LLM-friendly schema summary based on docs/design/测试数据域-本体Schema.md.

    领域迁移（2026-09-12）：路径由原先的 `parents[6]`（指向不存在的 `agentix/docs/`，
    使本函数始终返回空串）修正为仓库根目录 `parents[7]`，让测试数据集 Schema 说明
    真正进入 text2cypher / guardrails 提示词。
    """
    doc_path = (
        Path(__file__).resolve().parents[7]
        / "docs"
        / "design"
        / "测试数据域-本体Schema.md"
    )
    if not doc_path.exists():
        return ""

    try:
        markdown = doc_path.read_text(encoding="utf-8")
    except OSError:
        return ""

    markdown = "\n".join(
        line for line in markdown.splitlines() if line.strip() != "** End Patch"
    )

    tables = _extract_table_blocks(markdown)

    node_header, node_rows = (
        _parse_markdown_table(tables[0]) if tables else ([], [])
    )
    relationship_header, relationship_rows = (
        _parse_markdown_table(tables[1]) if len(tables) > 1 else ([], [])
    )

    summary_lines: list[str] = []

    if node_rows:
        summary_lines.append("Legal KG node types:")
        for cells in node_rows:
            label = _lookup_cell(cells, node_header, ["Label"], fallback_index=0)
            meaning = _lookup_cell(
                cells, node_header, [COL_CHINESE_MEANING], fallback_index=1
            )
            properties = _lookup_cell(
                cells, node_header, [COL_MAIN_PROPERTIES], fallback_index=2
            )
            note = _lookup_cell(
                cells, node_header, [COL_NOTE_EXAMPLE], fallback_index=3
            )

            if not label:
                continue

            bullet = label
            if meaning:
                bullet = f"{bullet} ({meaning})"

            details: list[str] = []
            if properties:
                details.append(f"properties: {properties}")
            if note:
                details.append(f"example: {note}")

            if details:
                bullet = f"{bullet}; " + "; ".join(details)

            summary_lines.append(f"- {bullet}")

    if relationship_rows:
        if summary_lines:
            summary_lines.append("")
        summary_lines.append("Legal KG relationship types:")
        for cells in relationship_rows:
            rel_type = _lookup_cell(
                cells, relationship_header, [COL_REL_TYPE], fallback_index=0
            )
            direction = _lookup_cell(
                cells,
                relationship_header,
                [COL_REL_DIRECTION, ALT_REL_DIRECTION],
                fallback_index=1,
            )
            meaning = _lookup_cell(
                cells, relationship_header, [COL_CHINESE_MEANING], fallback_index=2
            )
            properties = _lookup_cell(
                cells, relationship_header, [COL_MAIN_PROPERTIES], fallback_index=3
            )

            if not rel_type:
                continue

            bullet = rel_type
            if direction:
                bullet = f"{bullet} ({direction})"

            details = []
            if meaning:
                details.append(f"meaning: {meaning}")
            if properties:
                details.append(f"properties: {properties}")

            if details:
                bullet = f"{bullet}; " + "; ".join(details)

            summary_lines.append(f"- {bullet}")

    attribute_section = _extract_markdown_section(markdown, SECTION_ATTRIBUTES)
    if attribute_section:
        if summary_lines:
            summary_lines.append("")
        summary_lines.append("Property notes:")
        summary_lines.append(attribute_section)

    return "\n".join(summary_lines).strip()


def retrieve_and_parse_schema_from_graph_for_prompts(graph: Neo4jGraph) -> str:
    """
    Retrieve the runtime schema, clean it for prompt usage, and enrich
    it with curated information from docs/legal_domain_schema.md.
    """
    schema: str = graph.get_schema

    if "CypherQuery" in schema:
        schema = re.sub(
            get_cypher_query_node_graph_schema(), r"\2", schema, flags=re.MULTILINE
        )

    schema = schema.replace("{", "[").replace("}", "]")

    doc_summary = _format_legal_schema_from_docs()
    if doc_summary:
        return f"{doc_summary}\n\n{schema}"

    return schema


def resolve_generation_schema(graph: Neo4jGraph) -> str:
    """text2cypher 生成提示 ``{schema}`` 值（Stage F2b-d·R-P2-3 schema 感知生成）。

    - flag-off（``KG_SCHEMA_AWARE_GENERATION_ENABLED`` 默认关）＝基线裸
      ``graph.schema`` 逐位等价；
    - flag-on ＝与校验同源的 ``retrieve_and_parse_schema_from_graph_for_prompts``
      （测试数据域文档摘要 + 发现态 schema，标签/属性/关系均来自库内发现而非
      静态中文标签描述）；
    - 发现/渲染异常 fail-open 回裸 ``graph.schema``，绝不打断生成链路。
    """
    if not bool(getattr(settings, "KG_SCHEMA_AWARE_GENERATION_ENABLED", False)):
        return graph.schema
    try:
        return retrieve_and_parse_schema_from_graph_for_prompts(graph)
    except Exception as exc:  # noqa: BLE001 - schema 渲染失败绝不打断生成
        logger.warning(
            "schema-aware generation render failed; fall back to raw graph.schema: %s",
            exc,
        )
        return graph.schema
