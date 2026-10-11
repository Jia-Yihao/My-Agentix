"""
法律法规知识图谱工具定义
基于法律本体（法规/法条/章节/案由/司法解释/制定机关）与 Neo4j 法律法规图谱模型
"""
from typing import Optional

from pydantic import BaseModel
from pydantic import Field


class cypher_query(BaseModel):
    """法律法规知识图谱 Cypher 查询工具

    当用户询问关于法条、案由、司法解释、制定机关、章节、引用关系等法律法规信息时，
    使用此工具生成 Cypher 查询语句。

    适用场景包括：
    - 法条内容查询（某部法规的具体条文、款项）
    - 案由适用查询（某案由对应哪些法条、构成要件）
    - 司法解释查询（某案由或法条对应的司法解释）
    - 制定机关查询（某部法规由哪个机关制定、发布文号）
    - 章节结构查询（法规的编/章/节层级）
    - 引用关系查询（法条之间的引用、上位法关系）
    - 基于属性的法条筛选（如：所有关于合同违约的条文）
    - 基于案由的法条推荐（如：劳动争议涉及哪些法律规定）

    此工具会利用 LLM 生成符合法律法规图谱结构的 Cypher 查询语句。
    """

    task: str = Field(..., description="法律法规相关的查询任务描述，LLM 会根据此任务生成 Cypher 查询语句")


class predefined_cypher(BaseModel):
    """预定义法律法规 Cypher 查询工具

    此工具包含预定义的 Cypher 查询语句，用于快速响应常见的法律法规查询需求。
    基于法律本体（法规/法条/章节/案由/司法解释/制定机关）与 Neo4j 法律法规图谱模型设计。

    根据用户问题类型，可以选择以下类别的查询（键名对应 legal_cypher_dict 中的定义）：

    1. 案由查法条 (article_by_cause)：
       - 根据案由（如合同违约、劳动争议）检索对应的法条与适用要点

    2. 法规+条号查法条内容 (article_by_no)：
       - 根据法规名称与条号检索该条的具体内容、关键词

    3. 法条互引 (articles_cited_by)：
       - 根据法规与条号检索该条引用（指向）的其他法条

    4. 案由查司法解释 (interpretation_by_cause)：
       - 根据案由检索相关的司法解释与适用说明

    5. 机关查规定 (docs_by_authority)：
       - 根据制定机关检索其发布的主要法规与司法解释

    6. 上位法查询 (upper_law_of)：
       - 查询某部法规的上位法、层级效力与引用来源

    7. 章节结构查询 (chapter_articles)：
       - 查询某部法规的章节层级与条文分布

    请根据用户的问题选择最合适的查询，并提供必要的参数（如 law_name / article_no / cause_name / authority_name 等）。
    """

    query: str = Field(..., description="预定义查询的标识符，对应 legal_cypher_dict 中的键")
    parameters: dict = Field(..., description="查询所需的参数字典，如 {'law_name': '民法典', 'article_no': '第577条'}")


class microsoft_graphrag_query(BaseModel):
    """GraphRAG 法律知识推理工具

    当用户提出需要深度推理、多跳查询或复杂分析的法律问题时，使用此工具。

    适用场景包括：
    - 需要综合多部法条信息进行关联分析的问题（如法条沿革、效力层级推演）
    - 复杂的法律适用推理（如：某行为同时涉及民事与行政责任的竞合分析）
    - 跨法条引用关系分析（如：上位法与下位法、特别法与一般法的适用顺序）
    - 需要对法律法规知识进行总结、归纳、对比的问题
    - 开放式的法律咨询与案例要点梳理

    此工具利用 Microsoft GraphRAG 技术进行图谱推理和知识生成。
    """
    query: str = Field(..., description="需要通过 GraphRAG 进行深度推理的复杂法律问题")


class text2sql_query(BaseModel):
    """结构化数据库查询工具

    当用户提出“问数”“统计”“报表”类问题，需要访问关系型数据库（如 MySQL、PostgreSQL）时使用。

    适用场景：
    - 法律法规、案件、文书、用户等结构化数据统计
    - 基于表字段的筛选、聚合、排序
    - 多表关联、分组统计、趋势分析

    工具参数可选地提供数据库连接信息；若为空，则使用系统默认连接。
    """

    task: str = Field(..., description="需要执行的结构化数据库查询任务描述")
    connection_id: Optional[int] = Field(
        default=None,
        description="数据库连接配置 ID，留空则使用默认连接",
    )
    db_type: str = Field(
        default="MySQL",
        description="数据库类型，例如 MySQL、PostgreSQL 等",
    )
    max_rows: int = Field(
        default=1000,
        description="结果预览的最大返回行数",
    )
    connection_string: Optional[str] = Field(
        default=None,
        description="直接传入的数据库连接字符串，存在时优先级高于 connection_id",
    )
