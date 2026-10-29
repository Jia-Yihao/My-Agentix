"""KG 工具参数 schema（工具契约数据，规范位在 tools 层）。

为什么住 ``agentix/tools/``（维护者裁决，返工 M2-4 的落位）：

1. **domain 层零第三方依赖**：``tests/test_p17_domain_layer.py`` 的
   ``FORBIDDEN_THIRD_PARTY`` 含 ``pydantic``，而 ``_domain_files()`` 对
   ``agentix/domain/**.py`` **零豁免**全量 AST 扫描。把 pydantic 参数模型放进
   domain 必红 ``test_domain_files_have_no_third_party_imports``；该守卫从「只允许
   一条例外」收紧为「一条都不允许」是刻意的，不得为落位而放宽或加豁免。
2. **参数 schema 是工具契约的一部分**，与 ``ToolSpec`` 同源同层；同层已有先例：
   ``agentix/tools/registry_init.py`` 就本地声明 ``_ReadSkillSchema`` /
   ``create_skill_draft`` 这类参数模型。契约数据留在契约装配点所在的层，
   不需要为了「拆一条边」把它挪进一个它本不该去的层。
3. **拆 ``tools → application`` 边不依赖 domain 落位**：``application → tools``
   是既有合法方向，``kg_tools_list.py`` 作为再导出 shim 从本文件取类即可，
   不会制造新的越界边。

**等价性红线**：类名 == 工具注册名——``selection._spec_for_schema`` 按 pydantic
``title``（默认 = 类名）回查 spec，风险闸/能力闸才对该工具真实生效；类名或字段
一变，``model_json_schema()`` 的 ``title`` 就变，风险闸静默 miss。
本文件的类**必须与工具注册名逐字一致**，改动须过
``tests/test_p13_registry.py`` / ``tests/test_p12_reuse.py`` 回归。
"""

from typing import Optional

from pydantic import BaseModel, Field


class cypher_query(BaseModel):
    """测试数据集知识图谱 Cypher 查询工具

    当用户询问关于数据条目、数据分类、示例说明、数据来源、章节、引用关系等测试数据信息时，
    使用此工具生成 Cypher 查询语句。

    适用场景包括：
    - 数据条目内容查询（某个数据集的具体条目、字段）
    - 数据分类查询（某分类对应哪些条目、构成要点）
    - 示例说明查询（某分类或条目对应的补充说明）
    - 数据来源查询（某数据集由哪个机构发布、数据编号）
    - 章节结构查询（数据集的结构层级）
    - 引用关系查询（数据条目之间的引用、上下级数据集关系）
    - 基于属性的条目筛选（如：所有关于示例文档异常的条目）
    - 基于分类的条目推荐（如：异常行为涉及哪些数据条目）

    此工具会利用 LLM 生成符合测试数据图谱结构的 Cypher 查询语句。
    """

    task: str = Field(..., description="测试数据相关的查询任务描述，LLM 会根据此任务生成 Cypher 查询语句")


class predefined_cypher(BaseModel):
    """预定义测试数据 Cypher 查询工具

    此工具包含预定义的 Cypher 查询语句，用于快速响应常见的测试数据查询需求。
    基于数据本体（数据集/数据条目/章节/数据分类/示例说明/数据来源）与 Neo4j 测试数据图谱模型设计。

    根据用户问题类型，可以选择以下类别的查询（键名对应预定义查询字典中的定义）：

    1. 分类查条目 (article_by_cause)：
       - 根据数据分类（如示例文档异常、流程问题）检索对应的数据条目与适用要点

    2. 数据集+条号查条目内容 (article_by_no)：
       - 根据数据集名称与条号检索该条的具体内容、关键词

    3. 条目互引 (articles_cited_by)：
       - 根据数据集与条号检索该条引用（指向）的其他数据条目

    4. 分类查示例说明 (interpretation_by_cause)：
       - 根据数据分类检索相关的示例说明与适用说明

    5. 来源查数据集 (docs_by_authority)：
       - 根据数据来源检索其发布的主要数据集与示例说明

    6. 上级数据集查询 (upper_law_of)：
       - 查询某个数据集的上级数据集、层级关系与引用来源

    7. 章节结构查询 (chapter_articles)：
       - 查询某个数据集的章节层级与条目分布

    请根据用户的问题选择最合适的查询，并提供必要的参数（参数名以查询模板中的占位符为准）。
    """

    query: str = Field(..., description="预定义查询的标识符，对应预定义查询字典中的键")
    parameters: dict = Field(..., description="查询所需的参数字典，键名与所选查询模板中的占位符一致")


class microsoft_graphrag_query(BaseModel):
    """GraphRAG 测试数据推理工具

    当用户提出需要深度推理、多跳查询或复杂分析的测试数据问题时，使用此工具。

    适用场景包括：
    - 需要综合多个数据条目信息进行关联分析的问题（如条目沿革、层级推演）
    - 复杂的数据适用推理（如：某行为同时涉及两类分类的交叉分析）
    - 跨条目引用关系分析（如：上下级数据集、通用规则与特殊规则的适用顺序）
    - 需要对测试数据知识进行总结、归纳、对比的问题
    - 开放式的数据问答与案例要点梳理

    此工具利用 Microsoft GraphRAG 技术进行图谱推理和知识生成。
    """
    query: str = Field(..., description="需要通过 GraphRAG 进行深度推理的复杂测试数据问题")


class text2sql_query(BaseModel):
    """结构化数据库查询工具

    当用户提出“问数”“统计”“报表”类问题，需要访问关系型数据库（如 MySQL、PostgreSQL）时使用。

    适用场景：
    - 数据集、数据条目、示例文档、用户等结构化数据统计
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
