"""
Domain knowledge for the test-dataset SQL schema.

These descriptions are derived from the factual documentation under:
- docs/legal_domain_schema.md
- docs/prd-legal-domain.md

We surface them to the LLM prompts so that the model has grounded context
about the actual tables, columns, and relationships available in the
test-dataset statistics database.
"""

# NOTE(R-11): 测试数据集统计库（text2sql）本期暂缓启用。以下为测试数据集领域 SQL schema 说明，
# 仅供 lg_builder.route_query 的 text2sql-query 分支兜底使用，逻辑保持不变。
from typing import Dict, List, Tuple

# Table level descriptions used to enrich schema prompts.
TABLE_DESCRIPTIONS: Dict[str, str] = {
    "laws": (
        "数据集主表，记录一个测试数据集的基础信息，如名称、生效日期、数据编号与数据层级。"
        "主键 id；上级数据集通过 upper_law_id 自关联。"
    ),
    "chapters": (
        "编/章/节结构表，记录数据集内部的层级标题。"
        "通过 law_id 关联所属数据集，parent_id 支持章节嵌套。"
    ),
    "articles": (
        "数据条目明细表，保存每条数据条目的条目编号、条目全文与关键词。"
        "通过 law_id 关联所属数据集，chapter_id 关联所属章节。"
    ),
    "article_citations": (
        "条目互引关联表，记录某条数据条目引用的其他条目。"
        "通过 article_id 连接来源条目，cited_article_id 连接被引条目。"
    ),
    "causes": (
        "数据分类表，存储问题类型/分类名称与类别。"
    ),
    "article_causes": (
        "数据条目与数据分类的关联表，标记某条数据条目适用的分类。"
        "通过 article_id 连接条目，cause_id 连接数据分类。"
    ),
    "interpretations": (
        "示例说明表，记录示例说明/补充说明的名称、来源机构、日期与编号。"
        "通过 authority_id 关联来源机构。"
    ),
    "article_interpretations": (
        "数据条目与示例说明的关联表，标记某条数据条目对应的示例说明。"
        "通过 article_id 连接条目，interpretation_id 连接示例说明。"
    ),
    "authorities": (
        "机构表，存储数据来源机构的规范化信息，包含名称与层级。"
    ),
    "law_authorities": (
        "数据集与来源机构的关联表，记录某个数据集的来源机构。"
        "通过 law_id 连接数据集，authority_id 连接机构。"
    ),
}

# Column level descriptions used when metadata is missing from Neo4j.
COLUMN_DESCRIPTIONS: Dict[Tuple[str, str], str] = {
    ("laws", "name"): "数据集名称（唯一），如「示例数据集 A」。",
    ("laws", "effective_date"): "生效日期。",
    ("laws", "doc_number"): "数据编号。",
    ("laws", "level"): "数据层级，取值以数据表实际枚举为准。",
    ("laws", "upper_law_id"): "上级数据集 laws.id 的自关联外键，无则为空。",
    ("chapters", "law_id"): "关联数据集 laws.id 的外键。",
    ("chapters", "parent_id"): "父章节 chapters.id 的自关联外键，顶层为空。",
    ("chapters", "title"): "章节标题。",
    ("chapters", "level"): "层级深度，取值 编/章/节。",
    ("articles", "law_id"): "关联数据集 laws.id 的外键。",
    ("articles", "chapter_id"): "关联章节 chapters.id 的外键，可为空。",
    ("articles", "article_no"): "条目编号，如 577。",
    ("articles", "content"): "条目全文。",
    ("articles", "keywords"): "关键词，分号分隔字符串。",
    ("article_citations", "article_id"): "来源条目 articles.id 的外键。",
    ("article_citations", "cited_article_id"): "被引条目 articles.id 的外键。",
    ("causes", "name"): "数据分类名称（唯一）。",
    ("causes", "category"): "数据分类类别，取值以数据表实际枚举为准。",
    ("article_causes", "article_id"): "关联条目 articles.id 的外键。",
    ("article_causes", "cause_id"): "关联数据分类 causes.id 的外键。",
    ("interpretations", "name"): "示例说明名称。",
    ("interpretations", "authority_id"): "关联来源机构 authorities.id 的外键。",
    ("interpretations", "publish_date"): "发布日期。",
    ("interpretations", "doc_number"): "编号。",
    ("article_interpretations", "article_id"): "关联条目 articles.id 的外键。",
    ("article_interpretations", "interpretation_id"): (
        "关联示例说明 interpretations.id 的外键。"
    ),
    ("authorities", "name"): "机构名称（唯一），如 示例来源机构。",
    ("authorities", "level"): "机构层级，如 国家级/省级。",
    ("law_authorities", "law_id"): "关联数据集 laws.id 的外键。",
    ("law_authorities", "authority_id"): "关联机构 authorities.id 的外键。",
}


RELATIONSHIP_FACTS: List[Dict[str, str]] = [
    {
        "source_table": "chapters",
        "source_column": "law_id",
        "target_table": "laws",
        "target_column": "id",
        "relationship_type": "many-to-one",
        "description": "每个章节归属一个数据集。",
    },
    {
        "source_table": "articles",
        "source_column": "law_id",
        "target_table": "laws",
        "target_column": "id",
        "relationship_type": "many-to-one",
        "description": "每条数据条目归属一个数据集，与章节搭配描述结构。",
    },
    {
        "source_table": "articles",
        "source_column": "chapter_id",
        "target_table": "chapters",
        "target_column": "id",
        "relationship_type": "many-to-one",
        "description": "数据条目归属章节，未归属时可为空。",
    },
    {
        "source_table": "article_citations",
        "source_column": "article_id",
        "target_table": "articles",
        "target_column": "id",
        "relationship_type": "many-to-one",
        "description": "条目互引的来源端。",
    },
    {
        "source_table": "article_causes",
        "source_column": "article_id",
        "target_table": "articles",
        "target_column": "id",
        "relationship_type": "many-to-one",
        "description": "数据条目适用的分类。",
    },
    {
        "source_table": "article_causes",
        "source_column": "cause_id",
        "target_table": "causes",
        "target_column": "id",
        "relationship_type": "many-to-one",
        "description": "数据分类维度信息，包含类别。",
    },
    {
        "source_table": "article_interpretations",
        "source_column": "interpretation_id",
        "target_table": "interpretations",
        "target_column": "id",
        "relationship_type": "many-to-one",
        "description": "数据条目对应的示例说明。",
    },
    {
        "source_table": "interpretations",
        "source_column": "authority_id",
        "target_table": "authorities",
        "target_column": "id",
        "relationship_type": "many-to-one",
        "description": "示例说明的来源机构。",
    },
    {
        "source_table": "law_authorities",
        "source_column": "authority_id",
        "target_table": "authorities",
        "target_column": "id",
        "relationship_type": "many-to-one",
        "description": "数据集的来源机构。",
    },
]


DOMAIN_SUMMARY = """
- 数据库为测试数据集场景，核心实体包含数据集 (laws)、章节 (chapters)、数据条目 (articles)、数据分类 (causes)、示例说明 (interpretations) 与机构 (authorities)。
- chapters.law_id / articles.law_id / law_authorities.law_id -> laws.id；articles.chapter_id -> chapters.id；
  article_citations.article_id / cited_article_id -> articles.id；article_causes.article_id -> articles.id、
  article_causes.cause_id -> causes.id；article_interpretations.article_id -> articles.id、
  article_interpretations.interpretation_id -> interpretations.id；interpretations.authority_id / law_authorities.authority_id -> authorities.id。
- laws.level 与 causes.category 的取值以数据表实际枚举为准，不要凭空假设。
- 按数据集筛选时请使用 laws.name（如「示例数据集 A」），不要构造不存在的 code 字段。
- articles.keywords 为分号分隔字符串，检索关键词时使用 LIKE '%关键词%' 并注意全角/半角。
- 若需要统计某个数据集的数据条目数量，可使用 COUNT(articles.id) 并按 laws.name 分组。
- 所有查询均应围绕真实存在的表与字段展开，避免凭空构造表名或字段。
""".strip()
