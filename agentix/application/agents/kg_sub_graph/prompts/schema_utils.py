"""
工具函数，用于安全地从Neo4j数据库提取和处理数据库结构信息
"""

import re
from typing import Optional, Dict, Any
from langchain_neo4j import Neo4jGraph

from agentix.application.agents.lg_prompts import (
    GUARDRAILS_GRAPH_PREFIX,
    GUARDRAILS_SCOPE_PREFIX,
)

def safe_get_schema(graph: Optional[Neo4jGraph] = None) -> str:
    """
    安全地从Neo4j数据库获取结构信息，处理所有可能的错误和模板变量冲突
    
    Args:
        graph: Neo4jGraph对象，如果为None则返回空字符串
        
    Returns:
        str: 处理后的数据库结构描述
    """
    if graph is None:
        return ""

    try:
        # 获取原始schema字符串
        schema: str = graph.get_schema

        # 过滤不相关的内部节点信息
        if "CypherQuery" in schema:
            schema = re.sub(
                r"^(- \*\*CypherQuery\*\*[\s\S]+?)(^Relationship properties|- \*)",
                r"\2",
                schema,
                flags=re.MULTILINE
            )

        # 转义所有花括号，避免与模板变量冲突
        # 例如: {name} -> {{name}}
        schema = schema.replace("{", "{{").replace("}", "}}")

        return schema
    except Exception as e:
        print(f"获取Neo4j数据库结构失败: {e}")
        return ""

def create_guardrails_context(
    graph: Optional[Neo4jGraph] = None,
    scope_description: Optional[str] = None
) -> Dict[str, Any]:
    """
    创建用于guardrails提示的上下文变量
    
    Args:
        graph: Neo4jGraph对象
        scope_description: 可选的范围描述
        
    Returns:
        Dict: 包含scope_context和graph_schema的字典
    """
    # 前缀字面量复用 lg_prompts 的共享常量（单一事实源，P2-3 残留收口）。
    # ⚠️ 本模块的历史拼接空白符与 guardrails/prompts.py 不同：尾部是 "\n\n"
    # 且 graph 前缀**无前导换行**（GUARDRAILS_GRAPH_PREFIX 自带前导 "\n"，故 [1:] 去掉）。
    # 唯一调用方 ps_genai_agents（遗留死代码家族）——为不改变生成 prompt 的字节，
    # 此处保持原空白符，不做「顺手统一」；拼接结果与原内联 f-string 逐字节一致
    # （守卫见 tests/test_p23::TestPromptPrefixSingleSource）。
    scope_context = ""
    if scope_description:
        scope_context = f"{GUARDRAILS_SCOPE_PREFIX}{scope_description}\n\n"

    # 准备图结构描述
    graph_schema = ""
    if graph:
        schema = safe_get_schema(graph)
        if schema:
            graph_schema = f"{GUARDRAILS_GRAPH_PREFIX[1:]}{schema}\n\n"

    return {
        "scope_context": scope_context,
        "graph_schema": graph_schema
    }
