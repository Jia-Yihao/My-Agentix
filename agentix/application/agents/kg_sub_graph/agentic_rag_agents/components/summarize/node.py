"""
This code is based on content found in the LangGraph documentation: https://python.langchain.com/docs/tutorials/graph/#advanced-implementation-with-langgraph
"""

from typing import Any, Callable, Coroutine, Dict, List

from langchain_core.language_models import BaseChatModel

from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.state import OverallState


def create_summarization_node(
    llm: BaseChatModel,
) -> Callable[[OverallState], Coroutine[Any, Any, dict[str, Any]]]:
    """
    Create a Summarization node for a LangGraph workflow.

    Parameters
    ----------
    llm : BaseChatModel
        The LLM do perform processing.

    Returns
    -------
    Callable[[OverallState], OutputState]
        The LangGraph node.
    """

    async def summarize(state: OverallState) -> Dict[str, Any]:
        """
        Summarize results of the performed Cypher queries.
        """
        tasks = state.get("tasks", [])
        cypher_entries = state.get("cyphers", [])

        narrative_sections: List[str] = []
        metric_sections: List[str] = []
        error_sections: List[str] = []

        def _format_rows(rows: List[dict[str, Any]]) -> str:
            if not rows:
                return ""
            if len(rows) == 1:
                row = rows[0]
                if len(row) == 1:
                    key, value = next(iter(row.items()))
                    return f"{key}：{value}"
                return "; ".join(f"{key}：{value}" for key, value in row.items())
            lines = []

            # 检查是否是法条列表（包含"法规"和"条号"，法律法规领域主路径）
            is_legal_articles = all(
                "法规" in row and "条号" in row
                for row in rows
                if isinstance(row, dict)
            )

            for idx, row in enumerate(rows, 1):
                if is_legal_articles and isinstance(row, dict):
                    # 法条：显示「《法规》第X条」与条文内容，便于溯源
                    law = row.get("法规", "")
                    article_no = row.get("条号", "")
                    content = row.get("内容", "")
                    if law:
                        label = f"《{law}》第{article_no}条"
                    else:
                        label = f"第{article_no}条"
                    lines.append(f"{label}：{content}" if content else label)
                else:
                    # 其他数据（案由 / 司法解释 / 机关等）：显示所有字段
                    row_desc = ", ".join(f"{key}：{value}" for key, value in row.items())
                    lines.append(f"{idx}. {row_desc}")
            return "\n".join(lines)

        for idx, cypher in enumerate(cypher_entries):
            if hasattr(cypher, "model_dump"):
                data = cypher.model_dump()
            elif isinstance(cypher, dict):
                data = cypher
            else:
                data = {}

            task_label = ""
            if idx < len(tasks):
                task_label = tasks[idx].question
            else:
                task_label = data.get("task") or ""

            records = data.get("records") or {}
            errors = data.get("errors") or []

            if errors:
                error_sections.append(f"{task_label}：{'；'.join(errors)}" if task_label else "；".join(errors))
                continue

            if not records:
                continue

            if isinstance(records, dict):
                if isinstance(records.get("result"), str) and records["result"].strip():
                    narrative_sections.append(records["result"].strip())

                answer = records.get("answer")
                if answer:
                    metric_sections.append(f"{task_label}：{answer}".strip())

                rows = records.get("rows")
                if isinstance(rows, list) and rows:
                    formatted_rows = _format_rows(rows)
                    if formatted_rows:
                        metric_sections.append(
                            f"{task_label}：\n{formatted_rows}".rstrip()
                            if task_label
                            else formatted_rows
                        )
            elif isinstance(records, list):
                # 如果 records 是列表，友好格式化输出
                formatted_rows = _format_rows(records)
                if formatted_rows:
                    metric_sections.append(
                        f"{task_label}：\n{formatted_rows}".rstrip()
                        if task_label
                        else formatted_rows
                    )
            else:
                metric_sections.append(str(records))

        sections: List[str] = []
        if narrative_sections:
            sections.append("### 川菜概览\n" + "\n\n".join(narrative_sections))
        if metric_sections:
            sections.append("### 数据统计\n" + "\n\n".join(metric_sections))
        if error_sections:
            sections.append("### 查询提示\n" + "\n".join(f"- {msg}" for msg in error_sections))

        summary = "\n\n".join(section for section in sections if section).strip()
        if not summary:
            summary = "No data to summarize."

        return {"summary": summary, "steps": ["summarize"]}

    return summarize
