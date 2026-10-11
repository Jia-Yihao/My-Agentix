"""F1c·A-P1-2 ``todo_write`` 工具声明（TodoWrite：整体替换式任务清单）。

- **声明**（spec + LLM schema）在本文件，由 ``registry_init`` 在
  ``AGENT_LLM_PLANNER_ENABLED=true`` 时挂进声明清单（flag-off 零注册，
  声明清单与基线逐项一致）；
- **实现**（``TodoStore`` JSON 落盘 + handler + 装配）在组合根
  ``application/services/todo_write.py``（capabilities 不反向依赖
  application——与 plan_mode / cron_tools 同款职责切分）。

治理语义：

- 名称 ``todo_write`` 命中 human_review 词表 ``write`` → 有效风险 HIGH；
  声明 ``L2`` → HIGH，两层一致（沿 ``write_task_artifact`` 先例——声明
  不低于名称启发式，避免「声明形同虚设」）；
- ``side_effect=True``：清单落盘是数据面副作用，禁 auto-retry（防重复
  整体替换）；
- schema 类名即工具名（title 契约——否则 ``selection._spec_for_schema``
  的 title 回查静默 miss，沿 kg_tools_list / read_artifact 约定）。
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field

from agentix.tools.definitions import ToolSpec

__all__ = [
    "todo_write",
    "TodoItem",
    "build_todo_write_spec",
]

#: 单条清单内容上限（字符；结构化输出与 handler 双侧校验同源）。
MAX_TODO_CONTENT = 500

#: 状态词表（pending / in_progress / completed——spec 原文三态）。
TODO_STATUS_PATTERN = r"^(pending|in_progress|completed)$"


class TodoItem(BaseModel):
    """``todo_write`` 的单条清单项。"""

    content: str = Field(
        ...,
        min_length=1,
        max_length=MAX_TODO_CONTENT,
        description=f"要做的事（非空，≤{MAX_TODO_CONTENT} 字符）",
    )
    status: str = Field(
        ...,
        pattern=TODO_STATUS_PATTERN,
        description="状态：pending（待办）/ in_progress（进行中，全表唯一）/ completed（完成）",
    )


class todo_write(BaseModel):
    """``todo_write`` 入参（类名即工具名：schema title 契约）。"""

    items: List[TodoItem] = Field(
        default_factory=list,
        description=(
            "**整体替换**当前 TodoWrite 清单（空数组 = 清空）。"
            "全表至多一条 in_progress；持久化到磁盘、跨进程可恢复。"
        ),
    )


def build_todo_write_spec() -> ToolSpec:
    """构造 ``todo_write`` 声明（L2：名称词表 write → HIGH，两层一致）。"""
    return ToolSpec(
        name="todo_write",
        risk_level="L2",  # 词表 write → HIGH；L2 → HIGH，两层一致
        control=(
            "需人工审批（HITL）：HUMAN_REVIEW_ENABLED=true 时有效风险 HIGH，"
            "未获人工批准不得执行（fail-closed）；清单仅落本地 JSON "
            "（data/todos/current.json，整体替换语义、回执含全表可审计）。"
        ),
        description=(
            "维护任务执行清单（TodoWrite）：**整体替换**当前清单，用于多步任务的"
            "进度跟踪与断点恢复。\n"
            "何时用：任务拆出 2 步以上、或需要跨轮维持「先做什么/正在做什么/已完成」"
            "心智模型时；每完成一步就更新一次清单（把该步置 completed、下一步置 "
            "in_progress）。\n"
            "何时不能用：单步问答（直接作答即可）；或想**追加**而不是替换——"
            "本工具按整表替换语义工作，未包含在 items 里的条目会被移除。\n"
            "约束：status ∈ pending/in_progress/completed；全表至多一条 in_progress；"
            "清单落盘持久化（进程重启后可恢复），失败时内存态保持、不吞你的更新。"
        ),
        parameters=todo_write.model_json_schema(),
        side_effect=True,  # 清单落盘 = 数据面副作用；禁止 auto-retry
        capabilities=("interaction",),
        timeout_ms=5_000,
    )
