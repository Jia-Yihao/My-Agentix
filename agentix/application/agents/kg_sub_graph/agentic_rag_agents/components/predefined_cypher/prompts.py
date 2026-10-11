"""predefined_cypher 组件的提示词模板。

从 ``utils.py`` 中抽出，集中管理参数提取相关的静态提示文本。
"""

# 参数提取：静态系统指令（置首，跨请求稳定）。
PARAM_EXTRACTION_SYSTEM_PROMPT = (
    "你是参数提取助手，从用户问题中提取指定参数，输出 JSON，不要额外说明。"
)

# 参数提取：人类消息模板，动态值在后（user_question / query_name / param_names）。
PARAM_EXTRACTION_HUMAN_PROMPT = """用户问题: {user_question}
查询类型: {query_name}
需要提取的参数: {param_names}

请以 JSON 返回，形如: {{"参数名": "参数值"}}"""
