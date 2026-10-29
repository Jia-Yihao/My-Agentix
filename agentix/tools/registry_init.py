"""工具注册中心装配（P1-3a）。

依据方案 [第4章]：ToolSpec.description 必须包含「何时用 / 何时不能用」的反例，
这是降低工具误选的首要手段（选错工具先查描述，再怀疑模型）。

设计约束（方案 P1-3 风险缓解）：
- import 期零外部依赖——本模块只声明 spec（纯数据）与 schema 类引用，
  不触碰 Neo4j / Redis / LLM；
- ``ensure_registered()`` 幂等，``get_active_tools()`` 内部自动调用，
  调用点无需关心注册时机。

**与 registry 全集的关系**：``agentix.infrastructure.tools`` 包的
``__init__.py`` 还注册了 ``search``（SearchTool 实例，非 pydantic 模型，
KG 子图 ``bind_tools``/``PydanticToolsParser`` 不可用）。因此
``get_active_tools()`` 不遍历 registry 全集，而是只遍历下方
**声明式清单**（``_DECLARATIONS``）——LLM 工具 schema 与可调用工具
实例两类注册项互不干扰。

新增一个 KG 工具只需改 **本文件 1 处**（``_build_declarations`` 追加一项），
其余链路（lg_builder / tool_selection / PydanticToolsParser）自动生效——
方案 P1-3 验收 1。
"""

from typing import List, Tuple, Type

from pydantic import BaseModel, Field

from agentix.config import settings
from agentix.tools.definitions import ToolSpec, registry

# 延迟导入使本模块 import 期完全无副作用（kg schema 类是纯 pydantic 模型定义，
# 规范位在同层兄弟模块 agentix/tools/kg_arg_schemas.py——参数 schema 是工具契约数据，
# 不下沉 domain（p17 守卫对 domain 零豁免第三方依赖），也不留在 application
# （拆 tools → application 边由该模块承接），但保持函数内导入以防其未来演进引入依赖链副作用）。


class _ReadSkillSchema(BaseModel):
    """``read_skill`` 入参（Phase 21；仅 ``SKILLS_ENABLED=true`` 进声明清单）。"""

    name: str = Field(description="Skill 名称（从「Skills」目录清单中取，未知名字返回可用清单）")


class create_skill_draft(BaseModel):
    """``create_skill_draft`` 入参（P6-6.3；仅 ``SKILLS_ENABLED=true`` 进声明清单）。

    类名 == 工具注册名：``selection._spec_for_schema`` 按 pydantic title
    （默认 = 类名）回查 spec，风险闸/能力闸才对该工具真实生效。
    """

    name: str = Field(
        description="Skill 名称（小写字母/数字/连字符，不含路径分隔符；与落盘目录同名）"
    )
    description: str = Field(description="何时用（一句话，≤120 字符）")
    anti_when: str = Field(description="何时不能用（一句话，≤240 字符；负面描述必填）")
    body: str = Field(description="Skill 正文 Markdown（≤8000 字符：步骤、约束、反例）")


def _import_schemas() -> Tuple[Type[BaseModel], ...]:
    from agentix.tools.kg_arg_schemas import (  # noqa: PLC0415
        cypher_query,
        microsoft_graphrag_query,
        predefined_cypher,
        text2sql_query,
    )
    return cypher_query, predefined_cypher, microsoft_graphrag_query, text2sql_query


def _build_declarations() -> List[Tuple[ToolSpec, Type[BaseModel]]]:
    """构建声明式工具清单：[(spec, schema 类), ...]，registry 与 active 列表的唯一数据源。"""
    cypher_query, predefined_cypher, microsoft_graphrag_query, text2sql_query = (
        _import_schemas()
    )
    declarations: List[Tuple[ToolSpec, Type[BaseModel]]] = [
        (
            ToolSpec(
                name="cypher_query",
                risk_level="L0",  # 只读 Cypher（指南 §2.1.3 分级表）
                description=(
                    "用 Cypher 查询测试数据知识图谱（数据集、数据条目、章节、数据分类、示例说明、数据来源、引用关系）。\n"
                    "何时用：需要按数据集名称、条号、数据分类、数据来源、章节层级、引用/上级数据集关系做"
                    "结构化精确查询时（如：示例数据集 A第577条内容、示例文档异常对应哪些数据条目）。\n"
                    "何时不能用：需要数据条目正文的语义/模糊检索或深度推理时——改用 microsoft_graphrag_query；"
                    "需要问数/统计/报表类聚合时——改用 text2sql_query。\n"
                    "注意：仅生成并执行只读 Cypher（MATCH/RETURN），禁止写操作。"
                ),
                parameters=cypher_query.model_json_schema(),
            ),
            cypher_query,
        ),
        (
            ToolSpec(
                name="predefined_cypher",
                risk_level="L0",  # 只读预定义查询
                description=(
                    "预定义 Cypher 查询集：数据分类查数据条目、数据集+条号查内容、数据条目互引、数据分类查示例说明、"
                    "机构查规定、上级数据集、章节结构 7 类高频查询的快速通道。\n"
                    "何时用：问题能明确对应到上述 7 类模板之一时（比 cypher_query 更快更稳）。\n"
                    "何时不能用：问题超出 7 类模板覆盖范围（如跨类别的组合查询、生僻属性筛选）——"
                    "改用 cypher_query；语义/推理类问题——改用 microsoft_graphrag_query。"
                ),
                parameters=predefined_cypher.model_json_schema(),
            ),
            predefined_cypher,
        ),
        (
            ToolSpec(
                name="microsoft_graphrag_query",
                risk_level="L0",  # 只读图谱推理
                description=(
                    "GraphRAG 测试数据知识推理：对图谱做多跳推理、关联分析与归纳总结。\n"
                    "何时用：需要综合多部数据条目做关联分析（数据条目沿革、数据层级推演、责任竞合分析）、"
                    "开放式数据问答、案例要点梳理时。\n"
                    "何时不能用：只需要查某一条数据条目的确切原文时——改用 cypher_query 或 predefined_cypher"
                    "（GraphRAG 生成的是推理摘要，不是条文原文，答条文原文属于误用）；"
                    "问数/统计类——改用 text2sql_query。"
                ),
                parameters=microsoft_graphrag_query.model_json_schema(),
                timeout_ms=60_000,  # A2b：收编后按 LLM 图谱推理时长放宽（治理链超时闸）
            ),
            microsoft_graphrag_query,
        ),
        (
            ToolSpec(
                name="text2sql_query",
                risk_level="L0",  # 只读 SELECT
                description=(
                    "结构化数据库（MySQL/PostgreSQL）查询：问数、统计、报表、聚合、多表关联。\n"
                    "何时用：问题以数量、占比、同比/环比、分组统计为目标，需要访问关系型数据时。\n"
                    "何时不能用：数据条目/数据分类/示例说明等图谱内容查询——改用 cypher_query 或"
                    " microsoft_graphrag_query（图谱在 Neo4j，不在关系库，用本工具必然查空）。\n"
                    "注意：仅只读 SELECT，禁止 INSERT/UPDATE/DELETE。"
                ),
                parameters=text2sql_query.model_json_schema(),
                timeout_ms=120_000,  # A2b：收编后按 LLM 生成+SQL 执行链时长放宽（治理链超时闸）
            ),
            text2sql_query,
        ),
    ]
    # Phase 21（Skills 渐进披露）：flag-on 才把 read_skill 挂进声明清单——
    # flag-off 时声明清单与 Phase 18 完全一致（Strangler 零变化）。
    if settings.SKILLS_ENABLED:
        declarations.append(
            (
                ToolSpec(
                    name="read_skill",
                    risk_level="L0",  # 只读：读 SKILL.md 正文
                    description=(
                        "读取一个 Skill（markdown 操作规程）的正文。\n"
                        "何时用：「Skills」目录列出的 name 与当前任务相关，需要其完整"
                        "步骤/约束/反例时。\n"
                        "何时不能用：name 不在目录清单内（本工具只读已注册 skill）；"
                        "或目录行已含足够信息、无需正文时（不要为「看看有什么」空读）。\n"
                        "注意：只读、零副作用；未知名字返回 found=false 与"
                        " available_skills 清单，不抛错。"
                    ),
                    parameters=_ReadSkillSchema.model_json_schema(),
                    capabilities=("skills",),
                ),
                _ReadSkillSchema,
            )
        )
        # P6-6.3（Procedural Memory 沉淀 Skill）：flag-on 才把
        # create_skill_draft 挂进声明清单——flag-off 与 Phase 21 逐字节一致。
        declarations.append(
            (
                ToolSpec(
                    name="create_skill_draft",
                    risk_level="L1",  # 写草稿（data/skills_drafts/，不触生产 skills/）
                    description=(
                        "把已验证的成功经验沉淀为 Skill 草稿（写入 data/skills_drafts/）。\n"
                        "何时用：同一类任务已成功跑通、值得固化为可复用规程时。\n"
                        "何时不能用：一次性任务或未验证的猜测（草稿不会被加载，别刷屏）；"
                        "或要改动已注册 skill（本工具只新建草稿，从不覆盖）。\n"
                        "注意：草稿隔离于 skills/，需人工评审搬入后才对模型生效；"
                        "同名草稿已存在时拒绝覆盖。"
                    ),
                    parameters=create_skill_draft.model_json_schema(),
                    capabilities=("skills",),
                    side_effect=True,  # 磁盘写入（草稿目录）
                    timeout_ms=10_000,
                ),
                create_skill_draft,
            )
        )
    # Phase 23（出站通知）：flag-on 才把 notify_user 挂进声明清单——
    # flag-off 时声明清单与 Phase 22 完全一致（Strangler 零变化）。
    if settings.NOTIFY_USER_ENABLED:
        from agentix.tools.adapters.notify_user import (  # noqa: PLC0415
            NotifyUserSchema,
            build_notify_user_spec,
        )

        declarations.append((build_notify_user_spec(), NotifyUserSchema))
    # P1（任务执行）：flag-on 才把两个任务写工具挂进声明清单——
    # flag-off 时声明清单与基线一致（Strangler 零变化）。
    if getattr(settings, "TASK_EXEC_ENABLED", False):
        from agentix.tools.adapters.task_writes import (  # noqa: PLC0415
            AppendTaskNoteSchema,
            WriteTaskArtifactSchema,
            build_append_task_note_spec,
            build_write_task_artifact_spec,
        )

        declarations.append((build_append_task_note_spec(), AppendTaskNoteSchema))
        declarations.append((build_write_task_artifact_spec(), WriteTaskArtifactSchema))
    # P3（渐进上下文）：read_artifact 取回工具——与写工具同 flag 挂载
    # （写→读闭环一体开/关）；F1b2·M-P1-3 起 ``MEMORY_INDEX_ENABLED`` 也
    # 声明它（记忆索引的 obs:// 指针要靠它取回）——两 flag 任一开即声明，
    # 默认全关 ⇒ 声明清单仍与基线逐位等价。
    if getattr(settings, "TASK_EXEC_ENABLED", False) or getattr(
        settings, "MEMORY_INDEX_ENABLED", False
    ):
        from agentix.tools.adapters.artifact_read import (  # noqa: PLC0415
            build_read_artifact_spec,
            read_artifact as ReadArtifactSchema,
        )

        declarations.append((build_read_artifact_spec(), ReadArtifactSchema))
    # P2（延迟工具面）：延迟列表非空才把 tool_search 挂进声明清单——
    # 空（默认）时声明清单与基线逐位等价（Strangler 零变化）。
    if deferred_tool_names():
        from agentix.tools.adapters.tool_search import (  # noqa: PLC0415
            build_tool_search_spec,
        )
        from agentix.tools.adapters.tool_search import tool_search as ToolSearchSchema  # noqa: PLC0415

        declarations.append((build_tool_search_spec(), ToolSearchSchema))
    # P4（岗位对照·异常闭环 4.3）：计划态工具——flag-on 才进声明清单——
    # flag-off 时声明清单与基线逐项一致（Strangler 零变化）。
    if getattr(settings, "AGENT_ROLLBACK_ENABLED", False):
        from agentix.tools.adapters.plan_mode import (  # noqa: PLC0415
            build_enter_plan_mode_spec,
            build_exit_plan_mode_spec,
            enter_plan_mode as EnterPlanModeSchema,
            exit_plan_mode as ExitPlanModeSchema,
        )

        declarations.append((build_enter_plan_mode_spec(), EnterPlanModeSchema))
        declarations.append((build_exit_plan_mode_spec(), ExitPlanModeSchema))
    # P5（岗位对照·Proactive 5.4）：cron 定时任务工具族——flag-on 才进声明
    # 清单——flag-off（默认）时声明清单与基线逐项一致（Strangler 零变化）。
    if getattr(settings, "CRON_ENABLED", False):
        from agentix.tools.adapters.cron_tools import (  # noqa: PLC0415
            build_cron_create_spec,
            build_cron_delete_spec,
            build_cron_list_spec,
            build_cron_run_spec,
            cron_create as CronCreateSchema,
            cron_delete as CronDeleteSchema,
            cron_list as CronListSchema,
            cron_run as CronRunSchema,
        )

        declarations.append((build_cron_create_spec(), CronCreateSchema))
        declarations.append((build_cron_list_spec(), CronListSchema))
        declarations.append((build_cron_delete_spec(), CronDeleteSchema))
        declarations.append((build_cron_run_spec(), CronRunSchema))
    # A2c（统一执行链 Stage A）：记忆三工具——flag-on 才进声明清单，
    # flag-off 时声明清单与基线逐项一致（Strangler 零变化）。
    if getattr(settings, "MEMORY_TOOLS_ENABLED", False):
        from agentix.tools.adapters.memory_tools import (  # noqa: PLC0415
            build_forget_spec,
            build_recall_spec,
            build_remember_spec,
            forget as ForgetMessageSchema,
            recall as RecallMessageSchema,
            remember as RememberMessageSchema,
        )

        declarations.append((build_remember_spec(), RememberMessageSchema))
        declarations.append((build_recall_spec(), RecallMessageSchema))
        declarations.append((build_forget_spec(), ForgetMessageSchema))
    # F1b2·M-P1-4（AGENTS 级联）：read_agents_file 按需取回工具——flag-on
    # 才进声明清单，flag-off（默认）声明清单与基线逐项一致（Strangler 零变化）。
    if getattr(settings, "AGENTS_CASCADE_ENABLED", False):
        from agentix.tools.adapters.agents_file_read import (  # noqa: PLC0415
            build_read_agents_file_spec,
            read_agents_file as ReadAgentsFileSchema,
        )

        declarations.append((build_read_agents_file_spec(), ReadAgentsFileSchema))
    # F1c·A-P1-2（LLM 规划 + TodoWrite）：flag-on 才把 todo_write 挂进声明
    # 清单——flag-off（默认）声明清单与基线逐项一致（Strangler 零变化）。
    if getattr(settings, "AGENT_LLM_PLANNER_ENABLED", False):
        from agentix.tools.adapters.todo_write import (  # noqa: PLC0415
            build_todo_write_spec,
            todo_write as TodoWriteSchema,
        )

        declarations.append((build_todo_write_spec(), TodoWriteSchema))
    return declarations


def deferred_tool_names() -> frozenset[str]:
    """P2：延迟工具名集合（``TOOL_DEFERRED_TOOLS`` 解析）。

    - 逗号分隔、去空白、忽略空段；``tool_search`` 自身**永不延迟**
      （召回工具被延迟 = 机制自锁，属配置笔误，静默剔除）；
    - 空（默认）⇒ ``frozenset()`` ⇒ 常驻列表与声明清单均与基线逐位等价。
    """
    names = {
        name.strip()
        for name in (getattr(settings, "TOOL_DEFERRED_TOOLS", "") or "").split(",")
        if name.strip()
    }
    names.discard("tool_search")
    return frozenset(names)


_DECLARATIONS: List[Tuple[ToolSpec, Type[BaseModel]]] = []


def _get_declarations() -> List[Tuple[ToolSpec, Type[BaseModel]]]:
    """懒加载声明清单（首次调用时构建并缓存）。"""
    if not _DECLARATIONS:
        _DECLARATIONS.extend(_build_declarations())
    return _DECLARATIONS


def get_declarations() -> Tuple[Tuple[ToolSpec, Type[BaseModel]], ...]:
    """声明清单的公共只读视图（``tool_search`` 召回、注册表对账共用唯一数据源）。"""
    return tuple(_get_declarations())


def ensure_registered() -> None:
    """幂等注册声明清单（逐项补缺：含 flag-on 后新增的 ``read_skill``）。"""
    declarations = _get_declarations()
    for spec, schema_cls in declarations:
        if registry.has_tool(spec.name):
            continue
        # 懒加载 factory：create 时才返回 schema 类（import 期不实例化任何对象）
        def _factory(cls: Type[BaseModel] = schema_cls) -> Type[BaseModel]:
            return cls

        registry.register(spec, factory=_factory)


def get_active_tools() -> List[Type[BaseModel]]:
    """返回当前启用的 LLM 工具 schema 类列表。

    两层裁剪（都只动 LLM 可见面，注册表 spec 永不删，便于随时恢复）：

    - ``settings.DISABLED_TOOLS``（逗号分隔工具名，如 ``"text2sql_query"``，
      空串/未设置 = 不裁）；
    - ``settings.TOOL_DEFERRED_TOOLS``（P2 延迟工具，空 = 不裁）——被延迟
      的工具退出常驻 schema，经 ``tool_search`` 按需召回（声明清单仍含它）。
    """
    declarations = _get_declarations()
    excluded = {
        name.strip()
        for name in (settings.DISABLED_TOOLS or "").split(",")
        if name.strip()
    } | set(deferred_tool_names())
    return [schema_cls for spec, schema_cls in declarations if spec.name not in excluded]
