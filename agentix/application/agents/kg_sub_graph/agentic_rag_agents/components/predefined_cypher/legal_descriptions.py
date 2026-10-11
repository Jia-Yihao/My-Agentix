"""法律法规预定义 Cypher 查询的描述信息（对应 legal_cypher_dict.py）。

为 ``predefined_cypher`` 工具的向量匹配器提供语义描述，帮助 LLM 根据用户提问
快速匹配合适的查询。键名必须与 ``legal_cypher_dict.predefined_cypher_dict`` 一致。
"""

# 案由 → 法条
ARTICLE_BY_CAUSE_DESCRIPTIONS = {
    "article_by_cause": "根据案由反查相关法条，适用于用户问某类纠纷（如合同违约、盗窃）适用哪些法律条文。",
    "causes_of_article": "查询某条具体法条对应的案由/纠纷类型，适用于想了解该条适用场景。",
}

# 法规 + 条号 → 法条内容
ARTICLE_BY_NO_DESCRIPTIONS = {
    "article_by_no": "按法规名与条号精确查询法条全文与关键词，适用于用户点名某法某条。",
    "articles_cited_by": "查询某法条引用了哪些其他法条，适用于了解法条之间的互引关系。",
}

# 案由 → 司法解释
INTERPRETATION_DESCRIPTIONS = {
    "interpretation_by_cause": "根据案由查找配套的司法解释（两高解释、批复等），适用于用户需要司法解释原文。",
}

# 机关 → 规定
AUTHORITY_DESCRIPTIONS = {
    "docs_by_authority": "根据制定/发布机关反查其制定的法规或司法解释，适用于问某机关（如最高法、市场监管总局）出台了哪些规定。",
    "upper_law_of": "查询某部法规的上位法（母法），适用于了解法律效力层级与渊源。",
}

# 结构浏览类
STRUCTURE_DESCRIPTIONS = {
    "chapter_articles": "列出某部法规的章节及其下法条，适用于按编章结构浏览法条。",
    "cause_list_by_category": "按民事/刑事/行政类别列出案由，适用于了解某类纠纷有哪些案由。",
    "keyword_articles": "按关键词检索法条，适用于用户用主题词（如赔偿、违约）查找相关条文。",
}


# 合并所有查询描述
QUERY_DESCRIPTIONS: dict = {}
QUERY_DESCRIPTIONS.update(ARTICLE_BY_CAUSE_DESCRIPTIONS)
QUERY_DESCRIPTIONS.update(ARTICLE_BY_NO_DESCRIPTIONS)
QUERY_DESCRIPTIONS.update(INTERPRETATION_DESCRIPTIONS)
QUERY_DESCRIPTIONS.update(AUTHORITY_DESCRIPTIONS)
QUERY_DESCRIPTIONS.update(STRUCTURE_DESCRIPTIONS)
