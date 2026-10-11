"""
Answer formatting node.
"""

import json
from typing import Any, Callable, Coroutine, Dict, List

from agentix.infrastructure.core.logger import get_logger

logger = get_logger(service="text2sql.answer_formatter")

# R-P2-4（F2b-c）：重试耗尽 / SQL 无效 ⇒ 显式失败答案 + 显式错误码，
# 绝不把未执行 SQL 包装成「查询结果摘要」（下游 tool 读 execution_error 落 errors）。
SQL_NOT_EXECUTED_ANSWER = "抱歉，SQL 多次生成与校验均未通过，未执行任何查询，无法给出结果。"
SQL_NOT_EXECUTED_ERROR = "SQL 校验未通过（重试耗尽），未执行查询"


def create_answer_formatter_node() -> Callable[[Dict[str, Any]], Coroutine[Any, Any, Dict[str, Any]]]:
    """Build a LangGraph node that composes the final answer for the user."""

    async def format_answer(state: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("-----格式化最终回答-----")

        execution_error = state.get("execution_error")
        sql_statement = state.get("sql_statement", "")
        results = state.get("execution_results") or []
        analysis_text = state.get("analysis_text") or ""
        visualization = state.get("visualization") or {}
        question = state.get("question", "")
        is_valid = bool(state.get("is_valid", False))

        if execution_error:
            answer = f"抱歉，执行 SQL 时出现错误：{execution_error}"
        elif not is_valid:
            # R-P2-4：走到这里 = 校验重试耗尽且从未执行（execute 节点
            # 只在 is_valid=True 时可达）。显式失败，不产出结果摘要。
            logger.warning("SQL 未通过校验（重试耗尽），显式失败不产出结果")
            answer = SQL_NOT_EXECUTED_ANSWER
            execution_error = SQL_NOT_EXECUTED_ERROR
        else:
            answer_lines: List[str] = []
            answer_lines.append(f"### 查询结果摘要\n问题：{question or '（未提供）'}")
            if analysis_text:
                answer_lines.append("\n---\n")
                answer_lines.append(analysis_text)
            if results:
                answer_lines.append("\n---\n### 结果预览")
                preview = json.dumps(results[:5], ensure_ascii=False, indent=2)
                answer_lines.append(f"```json\n{preview}\n```")
            if visualization:
                answer_lines.append("\n---\n### 可视化建议")
                answer_lines.append(
                    f"- 类型：{visualization.get('chart_type', 'table')}\n"
                    f"- 标题：{visualization.get('title', '查询结果')}"
                )
                config = visualization.get("config") if isinstance(visualization, dict) else None
                if config:
                    answer_lines.append("```json")
                    answer_lines.append(json.dumps(config, ensure_ascii=False, indent=2))
                    answer_lines.append("```")
            answer = "\n".join(answer_lines).strip()

        viz_config = None
        if isinstance(visualization, dict):
            viz_config = visualization.get("config")

        return {
            "answer": answer,
            "sql_statement": sql_statement,
            "execution_results": results,
            "execution_error": execution_error,
            "visualization": visualization if isinstance(visualization, dict) else None,
            "visualization_config": viz_config,
            "steps": ["format_answer"],
        }

    return format_answer
