"""测试数据集预定义 Cypher 查询字典。

对应 kg_tools_list.py 中 predefined_cypher 工具描述的若干类查询。
所有查询使用参数化过滤（参数名与图谱 schema 中的中文占位符一致），
避免硬编码。变量名保持 ``predefined_cypher_dict`` 以便最小化导入改动。
"""

from typing import Dict

predefined_cypher_dict: Dict[str, str] = {
    # ==================== 1. 数据分类 → 数据条目 ====================
    "article_by_cause": """
MATCH (f:法条)-[:对应案由]->(:案由 {名称: $案由名})
RETURN f.法规 AS 法规, f.条号 AS 条号, f.内容 AS 内容
ORDER BY f.法规, f.条号 LIMIT 15
""",

    # ==================== 2. 数据集 + 条号 → 数据条目内容 ====================
    "article_by_no": """
MATCH (f:法条 {条号: $条号, 法规: $法规名})
RETURN f.法规 AS 法规, f.条号 AS 条号, f.内容 AS 内容, f.关键词 AS 关键词
""",

    # ==================== 3. 数据条目互引 ====================
    "articles_cited_by": """
MATCH (f:法条 {条号: $条号, 法规: $法规名})-[:引用]->(t:法条)
RETURN t.法规 AS 法规, t.条号 AS 条号, t.内容 AS 内容
ORDER BY t.条号 LIMIT 15
""",

    # ==================== 4. 数据分类 → 示例说明 ====================
    "interpretation_by_cause": """
MATCH (f:法条)-[:对应案由]->(:案由 {名称: $案由名})
MATCH (f)<-[:有司法解释]-(e:司法解释)
RETURN e.name AS 司法解释, e.发布机关 AS 发布机关, e.文号 AS 文号, e.日期 AS 日期
""",

    # ==================== 5. 机构 → 制定的数据集/示例说明 ====================
    "docs_by_authority": """
MATCH (o:机关 {名称: $机关名})-[r:制定机关]->(doc)
WHERE doc:法规 OR doc:司法解释
RETURN labels(doc)[0] AS 类型, doc.name AS 名称, doc.发文字号 AS 发文字号
ORDER BY 类型 LIMIT 20
""",

    # ==================== 6. 上级数据集 ====================
    "upper_law_of": """
MATCH (f:法规 {名称: $法规名})-[:上位法]->(p)
RETURN p.name AS 上位法名称, p.效力层级 AS 效力层级
""",

    # ==================== 7. 数据集的章节与数据条目 ====================
    "chapter_articles": """
MATCH (c:章节)-[:属于]->(:法规 {name: $法规名})
MATCH (a:法条)-[:属于]->(c)
RETURN c.标题 AS 章节, c.层级 AS 层级, a.条号 AS 条号, a.内容 AS 内容
ORDER BY c.层级, a.条号 LIMIT 30
""",

    # ==================== 8. 按类别列数据分类 ====================
    "cause_list_by_category": """
MATCH (ca:案由 {类别: $类别})
RETURN ca.名称 AS 案由, ca.类别 AS 类别
ORDER BY ca.名称 LIMIT 50
""",

    # ==================== 9. 关键词 → 数据条目 ====================
    "keyword_articles": """
MATCH (f:法条)
WHERE $关键词 IN f.关键词
RETURN f.法规 AS 法规, f.条号 AS 条号, f.内容 AS 内容
ORDER BY f.法规, f.条号 LIMIT 15
""",

    # ==================== 10. 数据条目对应的数据分类 ====================
    "causes_of_article": """
MATCH (f:法条 {条号: $条号, 法规: $法规名})-[:对应案由]->(ca:案由)
RETURN ca.名称 AS 案由, ca.类别 AS 类别
""",
}
