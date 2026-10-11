"""
Domain knowledge for the legal SQL schema.

These descriptions are derived from the factual documentation under:
- docs/legal_domain_schema.md
- docs/prd-legal-domain.md

We surface them to the LLM prompts so that the model has grounded context
about the actual tables, columns, and relationships available in the
legal statistics database.
"""

# NOTE(R-11): 法律统计库（text2sql）本期暂缓启用。以下为法律法规领域 SQL schema 说明，
# 仅供 lg_builder.route_query 的 text2sql-query 分支兜底使用，逻辑保持不变。
from typing import Dict, List, Tuple

# Table level descriptions used to enrich schema prompts.
TABLE_DESCRIPTIONS: Dict[str, str] = {
    "laws": (
        "法规主表，记录一部法律/法规的基础信息，如名称、生效日期、发文字号与效力层级。"
        "主键 id；上位法通过 upper_law_id 自关联。"
    ),
    "chapters": (
        "编/章/节结构表，记录法规内部的层级标题。"
        "通过 law_id 关联所属法规，parent_id 支持章节嵌套。"
    ),
    "articles": (
        "法条明细表，保存每条法条的条号、条文全文与关键词。"
        "通过 law_id 关联所属法规，chapter_id 关联所属章节。"
    ),
    "article_citations": (
        "法条互引关联表，记录某条法条引用的其他法条。"
        "通过 article_id 连接来源法条，cited_article_id 连接被引法条。"
    ),
    "causes": (
        "案由表，存储案件事由/纠纷类型的名称与类别（民事/刑事/行政）。"
    ),
    "article_causes": (
        "法条与案由的关联表，标记某条法条适用的纠纷类型。"
        "通过 article_id 连接法条，cause_id 连接案由。"
    ),
    "interpretations": (
        "司法解释表，记录两高司法解释/批复的名称、发布机关、日期与文号。"
        "通过 authority_id 关联发布机关。"
    ),
    "article_interpretations": (
        "法条与司法解释的关联表，标记某条法条对应的司法解释。"
        "通过 article_id 连接法条，interpretation_id 连接司法解释。"
    ),
    "authorities": (
        "机关表，存储制定/发布机关的规范化信息，包含名称与层级。"
    ),
    "law_authorities": (
        "法规与制定机关的关联表，记录某部法规的制定机关。"
        "通过 law_id 连接法规，authority_id 连接机关。"
    ),
}

# Column level descriptions used when metadata is missing from Neo4j.
COLUMN_DESCRIPTIONS: Dict[Tuple[str, str], str] = {
    ("laws", "name"): "法规名称（唯一），如《民法典》。",
    ("laws", "effective_date"): "生效日期。",
    ("laws", "doc_number"): "发文字号。",
    ("laws", "level"): "效力层级，如 法律/行政法规/地方性法规。",
    ("laws", "upper_law_id"): "上位法 laws.id 的自关联外键，无则为空。",
    ("chapters", "law_id"): "关联法规 laws.id 的外键。",
    ("chapters", "parent_id"): "父章节 chapters.id 的自关联外键，顶层为空。",
    ("chapters", "title"): "章节标题。",
    ("chapters", "level"): "层级深度，取值 编/章/节。",
    ("articles", "law_id"): "关联法规 laws.id 的外键。",
    ("articles", "chapter_id"): "关联章节 chapters.id 的外键，可为空。",
    ("articles", "article_no"): "条号，如 577。",
    ("articles", "content"): "条文全文。",
    ("articles", "keywords"): "关键词，分号分隔字符串。",
    ("article_citations", "article_id"): "来源法条 articles.id 的外键。",
    ("article_citations", "cited_article_id"): "被引法条 articles.id 的外键。",
    ("causes", "name"): "案由名称（唯一）。",
    ("causes", "category"): "案由类别，取值 民事/刑事/行政。",
    ("article_causes", "article_id"): "关联法条 articles.id 的外键。",
    ("article_causes", "cause_id"): "关联案由 causes.id 的外键。",
    ("interpretations", "name"): "司法解释名称。",
    ("interpretations", "authority_id"): "关联发布机关 authorities.id 的外键。",
    ("interpretations", "publish_date"): "发布日期。",
    ("interpretations", "doc_number"): "文号。",
    ("article_interpretations", "article_id"): "关联法条 articles.id 的外键。",
    ("article_interpretations", "interpretation_id"): (
        "关联司法解释 interpretations.id 的外键。"
    ),
    ("authorities", "name"): "机关名称（唯一），如 最高人民法院。",
    ("authorities", "level"): "机关层级，如 国家级/省级。",
    ("law_authorities", "law_id"): "关联法规 laws.id 的外键。",
    ("law_authorities", "authority_id"): "关联机关 authorities.id 的外键。",
}


RELATIONSHIP_FACTS: List[Dict[str, str]] = [
    {
        "source_table": "chapters",
        "source_column": "law_id",
        "target_table": "laws",
        "target_column": "id",
        "relationship_type": "many-to-one",
        "description": "每个章节归属一部法规。",
    },
    {
        "source_table": "articles",
        "source_column": "law_id",
        "target_table": "laws",
        "target_column": "id",
        "relationship_type": "many-to-one",
        "description": "每条法条归属一部法规，与章节搭配描述结构。",
    },
    {
        "source_table": "articles",
        "source_column": "chapter_id",
        "target_table": "chapters",
        "target_column": "id",
        "relationship_type": "many-to-one",
        "description": "法条归属章节，未归属时可为空。",
    },
    {
        "source_table": "article_citations",
        "source_column": "article_id",
        "target_table": "articles",
        "target_column": "id",
        "relationship_type": "many-to-one",
        "description": "法条互引的来源端。",
    },
    {
        "source_table": "article_causes",
        "source_column": "article_id",
        "target_table": "articles",
        "target_column": "id",
        "relationship_type": "many-to-one",
        "description": "法条适用的纠纷类型。",
    },
    {
        "source_table": "article_causes",
        "source_column": "cause_id",
        "target_table": "causes",
        "target_column": "id",
        "relationship_type": "many-to-one",
        "description": "案由维度信息，包含类别。",
    },
    {
        "source_table": "article_interpretations",
        "source_column": "interpretation_id",
        "target_table": "interpretations",
        "target_column": "id",
        "relationship_type": "many-to-one",
        "description": "法条对应的司法解释。",
    },
    {
        "source_table": "interpretations",
        "source_column": "authority_id",
        "target_table": "authorities",
        "target_column": "id",
        "relationship_type": "many-to-one",
        "description": "司法解释的发布机关。",
    },
    {
        "source_table": "law_authorities",
        "source_column": "authority_id",
        "target_table": "authorities",
        "target_column": "id",
        "relationship_type": "many-to-one",
        "description": "法规的制定机关。",
    },
]


DOMAIN_SUMMARY = """
- 数据库为法律法规场景，核心实体包含法规 (laws)、章节 (chapters)、法条 (articles)、案由 (causes)、司法解释 (interpretations) 与机关 (authorities)。
- chapters.law_id / articles.law_id / law_authorities.law_id -> laws.id；articles.chapter_id -> chapters.id；
  article_citations.article_id / cited_article_id -> articles.id；article_causes.article_id -> articles.id、
  article_causes.cause_id -> causes.id；article_interpretations.article_id -> articles.id、
  article_interpretations.interpretation_id -> interpretations.id；interpretations.authority_id / law_authorities.authority_id -> authorities.id。
- laws.level 的取值限定为 法律/行政法规/地方性法规；causes.category 的取值限定为 民事/刑事/行政。
- 按法规筛选时请使用 laws.name（如《民法典》），不要构造不存在的 code 字段。
- articles.keywords 为分号分隔字符串，检索关键词时使用 LIKE '%关键词%' 并注意全角/半角。
- 若需要统计某部法规的法条数量，可使用 COUNT(articles.id) 并按 laws.name 分组。
- 所有查询均应围绕真实存在的表与字段展开，避免凭空构造表名或字段。
""".strip()
