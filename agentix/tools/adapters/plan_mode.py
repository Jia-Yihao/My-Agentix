"""P4 ``enter_plan_mode`` / ``exit_plan_mode`` 计划态工具声明（岗位对照·异常闭环 4.3）。

借鉴 claw-code ``tools/src/lib.rs:6013-6081``「计划态 = 可回滚的**配置级**权限切换」：

- **声明**（spec + LLM schema）在本文件，由 ``registry_init`` 在
  ``AGENT_ROLLBACK_ENABLED=true`` 时挂进声明清单（flag-off 零注册，声明清单
  与基线逐项一致）；
- **实现**（handler → ``RollbackPort`` 快照/恢复 + ``executor.policy`` 切换）
  在组合根 ``application/services/rollback.py``（capabilities 不反向依赖
  agent/application——与 notify_user / read_skill / task_writes 同款职责切分）。

治理语义：

- ``enter_plan_mode``：**快照当前策略 → 收紧到 L0 只读**（计划态内只允许
  只读探查——权限切换只收紧不放松，本身无需审批）；名称不命中
  ``human_review.py`` 词表（无 write/drop/send 等词）→ 有效风险 LOW；
- ``exit_plan_mode``：按 checkpoint **恢复快照时的原策略**（保存/恢复语义）；
  名称同样不命中词表 → LOW；
- 两工具均 ``side_effect=True``：切换运行期策略状态是配置级副作用
  （非只读数据面），且 side-effectful 工具禁止 auto-retry（防重复切换）；
- schema 类名即工具名（title 契约——否则 ``selection._spec_for_schema``
  的 title 回查静默 miss，沿用 kg_tools_list / read_artifact 约定）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from agentix.tools.definitions import ToolSpec

__all__ = [
    "enter_plan_mode",
    "exit_plan_mode",
    "build_enter_plan_mode_spec",
    "build_exit_plan_mode_spec",
]

#: 进入计划态的原因上限（回显进返回值/轨迹，有界）。
MAX_REASON_CHARS = 400


class enter_plan_mode(BaseModel):
    """``enter_plan_mode`` 入参（类名即工具名：schema title 契约）。"""

    reason: str = Field(
        default="",
        description=f"进入计划态的原因（可选，回显进返回值与轨迹，≤{MAX_REASON_CHARS} 字符）",
    )


class exit_plan_mode(BaseModel):
    """``exit_plan_mode`` 入参（类名即工具名：schema title 契约）。"""

    checkpoint_id: str = Field(
        default="",
        description=(
            "enter_plan_mode 返回的 checkpoint_id（留空 = 使用当前计划态的检查点；"
            "传入不存在的 id 会诚实失败）"
        ),
    )


def build_enter_plan_mode_spec() -> ToolSpec:
    """构造 ``enter_plan_mode`` 声明（L0：只收紧权限，不放松）。"""
    return ToolSpec(
        name="enter_plan_mode",
        risk_level="L0",  # 快照→收紧到 L0：纯收敛动作，免审批
        description=(
            "进入「计划态」：把当前工具权限策略快照为检查点，并把执行权限收紧到 "
            "L0 只读（计划态内只允许只读工具）。\n"
            "何时用：任务进入需要「先看清再动手」的阶段——先只读探查现状、评估方案，"
            "确认无误后再退出计划态执行变更。\n"
            "何时不能用：已处于计划态（本工具幂等，直接返回既有 checkpoint_id，"
            "不会重复快照，也不会覆盖原检查点）；或当前步骤本身就需要写操作"
            "（先 exit_plan_mode 恢复权限再执行）。\n"
            "注意：本工具只收紧权限（快照 → L0），不产生任何数据变更；"
            "与 exit_plan_mode 成对使用，退出时按 checkpoint 恢复进入前的原策略"
            "（保存/恢复语义，重启即失——检查点存进程内内存）。"
        ),
        parameters=enter_plan_mode.model_json_schema(),
        side_effect=True,  # 运行期策略切换 = 配置级副作用；禁止 auto-retry
        capabilities=("control",),
        timeout_ms=5_000,
    )


def build_exit_plan_mode_spec() -> ToolSpec:
    """构造 ``exit_plan_mode`` 声明（L0：按快照恢复原策略）。"""
    return ToolSpec(
        name="exit_plan_mode",
        risk_level="L0",  # 恢复快照（快照本身就是进入前的原策略）；免审批
        description=(
            "退出「计划态」：按 checkpoint 把工具权限策略恢复为进入计划态前的原状态"
            "（保存/恢复语义）。\n"
            "何时用：计划态内的只读探查已完成、确认方案无误，需要恢复写权限执行变更；"
            "或计划态内发现误入需要立即退出。\n"
            "何时不能用：从未进入计划态（本工具诚实失败，不会凭空改策略）；"
            "或 checkpoint_id 不在库中（传错 id 同样诚实失败，不静默降级）。\n"
            "注意：恢复值即 enter 时快照的原策略（默认无策略约束时恢复为无约束，"
            "不会偷偷保留 L0）；退出后计划态标记清除，可再次 enter。"
        ),
        parameters=exit_plan_mode.model_json_schema(),
        side_effect=True,  # 运行期策略切换 = 配置级副作用；禁止 auto-retry
        capabilities=("control",),
        timeout_ms=5_000,
    )
