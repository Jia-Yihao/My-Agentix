"""测试数据集预定义 Cypher 查询的描述信息（对应 legal_cypher_dict.py）。

为 ``predefined_cypher`` 工具的向量匹配器提供语义描述，帮助 LLM 根据用户提问
快速匹配合适的查询。键名必须与 ``legal_cypher_dict.predefined_cypher_dict`` 一致。
"""

# 数据分类 → 数据条目
ARTICLE_BY_CAUSE_DESCRIPTIONS = {
    "article_by_cause": "根据数据分类反查相关数据条目，适用于用户问某类问题（如示例文档违约、异常行为）适用哪些数据条目。",
    "causes_of_article": "查询某条具体数据条目对应的数据分类，适用于想了解该条目的适用场景。",
}

# 数据集 + 条目编号 → 条目内容
ARTICLE_BY_NO_DESCRIPTIONS = {
    "article_by_no": "按数据集名与条目编号精确查询条目全文与关键词，适用于用户点名某个数据集的某条条目。",
    "articles_cited_by": "查询某条数据条目引用了哪些其他条目，适用于了解条目之间的互引关系。",
}

# 数据分类 → 示例说明
INTERPRETATION_DESCRIPTIONS = {
    "interpretation_by_cause": "根据数据分类查找配套的示例说明（补充说明、批复等），适用于用户需要示例说明原文。",
}

# 机构 → 说明文档
AUTHORITY_DESCRIPTIONS = {
    "docs_by_authority": "根据来源/发布机构反查其发布的数据集或示例说明，适用于问某机构（如示例来源机构）出台了哪些说明。",
    "upper_law_of": "查询某个数据集的上级数据集，适用于了解数据层级与渊源。",
}

# 结构浏览类
STRUCTURE_DESCRIPTIONS = {
    "chapter_articles": "列出某个数据集的章节及其下数据条目，适用于按编章结构浏览条目。",
    "cause_list_by_category": "按数据类别列出数据分类，适用于了解某类问题有哪些分类。",
    "keyword_articles": "按关键词检索数据条目，适用于用户用主题词（如赔偿、违约）查找相关条目。",
}


# 合并所有查询描述
QUERY_DESCRIPTIONS: dict = {}
QUERY_DESCRIPTIONS.update(ARTICLE_BY_CAUSE_DESCRIPTIONS)
QUERY_DESCRIPTIONS.update(ARTICLE_BY_NO_DESCRIPTIONS)
QUERY_DESCRIPTIONS.update(INTERPRETATION_DESCRIPTIONS)
QUERY_DESCRIPTIONS.update(AUTHORITY_DESCRIPTIONS)
QUERY_DESCRIPTIONS.update(STRUCTURE_DESCRIPTIONS)
