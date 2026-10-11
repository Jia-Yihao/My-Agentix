"""A2c 记忆三工具（``remember`` / ``recall`` / ``forget``，``MEMORY_TOOLS_ENABLED``）。

分层同 ``notify_user``/``task_writes``：spec + LLM schema（pydantic）在本模块，
flag-off 不进声明清单（``registry_init`` gate）；handler 安装在
``application/services/memory_tools_bridge.py``（组合根 flag-on 接线）。

风险分级：``remember``/``forget`` 是记忆库写操作（side_effect=True，L1）；
``recall`` 只读（L0）。执行经 ``ToolExecutor`` ⇒ 风险/权限/HITL/超时闸门全在前。
"""

from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, Field

from agentix.tools.definitions import ToolSpec

#: 记忆 scope 取值（与 ``domain.agent_types.MemoryScope`` 对齐）
_ScopeLiteral = Literal["user", "knowledge", "task"]
#: 记忆 kind 取值（与 ``domain.agent_types.MemoryKind`` 对齐）
_KindLiteral = Literal["working", "episodic", "semantic", "procedural", "user"]


class remember(BaseModel):
    """``remember`` 入参（类名 == 工具注册名：title 契约见 registry_init 注释）。"""

    content: str = Field(description="要记住的内容（一句话事实/偏好/指令；入库前经 PII 脱敏）")
    subject: str = Field(
        default="default",
        description="记忆主体（scope=user 时为 user_id；请求未携带标识时缺省 default）",
    )
    scope: _ScopeLiteral = Field(default="user", description="记忆空间：user=用户画像 / knowledge=知识 / task=任务")
    kind: _KindLiteral = Field(default="semantic", description="条目类型：semantic=事实偏好 / episodic=事件 / procedural=流程 / user=用户特质 / working=工作记忆")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="置信度 0-1（冲突裁决与排序依据）")
    tags: List[str] = Field(default_factory=list, description="标签（用于 recall 关键词过滤）")


class recall(BaseModel):
    """``recall`` 入参（类名 == 工具注册名）。"""

    query: str = Field(default="", description="过滤关键词（匹配内容与标签；空 = 不过滤）")
    subject: str = Field(default="default", description="记忆主体（与 remember 一致；scope=user 时为 user_id）")
    scope: _ScopeLiteral = Field(default="user", description="记忆空间：user / knowledge / task")
    limit: int = Field(default=10, ge=1, le=50, description="最多返回条数（按置信度降序截断）")


class forget(BaseModel):
    """``forget`` 入参（类名 == 工具注册名）。"""

    content: str = Field(default="", description="要删除的条目内容（与存储内容精确匹配；与 item_id 至少给一个）")
    item_id: str = Field(default="", description="条目 id（优先于内容匹配；来自 remember/recall 返回）")
    subject: str = Field(default="default", description="记忆主体（与 remember 一致）")
    scope: _ScopeLiteral = Field(default="user", description="记忆空间：user / knowledge / task")


def build_remember_spec() -> ToolSpec:
    return ToolSpec(
        name="remember",
        risk_level="L1",  # 记忆库写入（side-effect）
        description=(
            "把一条长期记忆写入记忆库（PII 脱敏后入库，同内容幂等覆盖）。\n"
            "何时用：用户明确表达偏好/约束/事实（「记住我喜欢…」「以后都…」），"
            "或任务中产生了值得跨会话保留的信息。\n"
            "何时不能用：一次性任务的中间结果（用 write_task_artifact）；"
            "未经用户确认的猜测（不要替用户「决定」他没说过的偏好）。\n"
            "注意：写操作（L1），会经风险/审批闸门；删除用 forget。"
        ),
        parameters=remember.model_json_schema(),
        capabilities=("memory",),
        side_effect=True,
    )


def build_recall_spec() -> ToolSpec:
    return ToolSpec(
        name="recall",
        risk_level="L0",  # 只读召回
        description=(
            "按关键词/主体召回长期记忆条目（按置信度降序，返回内容+标签+id）。\n"
            "何时用：任务开始或需要用户历史偏好/既往事实时，先召回再行动；"
            "或用户问「你之前记得…」类问题。\n"
            "何时不能用：查任务产物（read_artifact）；查知识库语料（检索路径）；"
            "空查询刷全量（limit 之外拿不到更多，别反复调用）。\n"
            "注意：只读；拿到 item_id 后可用 forget 删除。"
        ),
        parameters=recall.model_json_schema(),
        capabilities=("memory",),
    )


def build_forget_spec() -> ToolSpec:
    return ToolSpec(
        name="forget",
        risk_level="L1",  # 记忆库删除（side-effect）
        description=(
            "删除一条长期记忆（按 item_id 或内容精确匹配；scope+subject 隔离）。\n"
            "何时用：用户明确要求「忘掉…/删除那条记忆」，或 remember 写入了错误内容需要撤回。\n"
            "何时不能用：不知道要删哪条（先 recall 拿 item_id）；"
            "或想清空全部记忆（本工具一次只删精确匹配的条目，不提供通配）。\n"
            "注意：删除不可恢复（L1），返回 deleted 数量；0 = 未匹配到。"
        ),
        parameters=forget.model_json_schema(),
        capabilities=("memory",),
        side_effect=True,
    )


__all__ = [
    "remember",
    "recall",
    "forget",
    "build_remember_spec",
    "build_recall_spec",
    "build_forget_spec",
]
