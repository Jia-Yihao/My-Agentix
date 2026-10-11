"""P5（岗位对照·Proactive 5.4）：cron 定时任务工具族声明。

借鉴 claw-code ``tools/src/lib.rs:484-1346`` 的 ``Cron*``/Task 工具族
（source-code-study §5 5.4）。职责切分与 ``plan_mode.py`` 同款：

- **声明**（spec + LLM schema）在本文件，由 ``registry_init`` 在
  ``CRON_ENABLED=true`` 时挂进声明清单（flag-off 零注册，声明清单与基线
  逐项一致）；
- **实现**（handler → ``JobStore`` / ``CronScheduler``）在组合根
  ``application/services/cron_scheduler.py``（capabilities 不反向依赖
  application——与 notify_user / plan_mode 同款职责切分）。

治理语义：

- ``cron_create`` / ``cron_delete`` / ``cron_run``：**L1 + side_effect=True**
  ——创建/删除/触发持久化定时任务是配置级副作用，禁止 auto-retry；
  L1 ⇒ 有效风险 MEDIUM ⇒ ``HUMAN_REVIEW_ENABLED`` 时须人工批准（与
  ``notify_user`` 同级）；
- ``cron_list``：L0 只读；
- ``cron_run`` 的 ``timeout_ms=130_000`` 必须大于 ``CRON_JOB_TIMEOUT_S``
  （执行半的 per-job 超时）——p58 有漂移守卫测试钉死；
- schema 类名即工具名（title 契约——否则 ``selection._spec_for_schema``
  的 title 回查静默 miss，沿用 read_artifact / plan_mode 约定）。

**安全边界**：本工具族**不提供任意 shell 执行**——确定性非模型任务走
``kind="tool"``（经 ``ToolExecutor`` 全闸门），模型任务走 ``kind="agent"``；
hermes 的 ``script``（no_agent 裸脚本）不在本仓复刻（出站即副作用：
调度任务绕过治理比缺能力更危险）。
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, Field

from agentix.tools.definitions import ToolSpec

__all__ = [
    "cron_create",
    "cron_list",
    "cron_delete",
    "cron_run",
    "build_cron_create_spec",
    "build_cron_list_spec",
    "build_cron_delete_spec",
    "build_cron_run_spec",
    "MAX_CRON_NAME_CHARS",
    "MAX_CRON_PROMPT_CHARS",
    "MAX_CRON_ARGS_BYTES",
]

#: 任务名上限（进 cron_list/通知，有界）。
MAX_CRON_NAME_CHARS = 64

#: agent 型任务提示词上限（一次性投喂，防无界 prompt 落盘）。
MAX_CRON_PROMPT_CHARS = 4000

#: tool 型任务入参序列化字节上限（防把大对象写进 jobs.json）。
MAX_CRON_ARGS_BYTES = 8192


class cron_create(BaseModel):
    """``cron_create`` 入参（类名即工具名：schema title 契约）。"""

    name: str = Field(
        ...,
        description=f"任务名（cron_list/通知中展示，≤{MAX_CRON_NAME_CHARS} 字符）",
    )
    expr: str = Field(
        ...,
        description=(
            "标准 5 段 cron 表达式（分 时 日 月 周，**UTC**），"
            "如 `0 9 * * 1-5`（工作日 09:00）"
        ),
    )
    prompt: str = Field(
        default="",
        description=(
            f"agent 型任务：定时执行的提示词（≤{MAX_CRON_PROMPT_CHARS} 字符；"
            "与 tool 二选一，prompt 与 tool 同时给会拒绝）"
        ),
    )
    tool: str = Field(
        default="",
        description="tool 型任务：已注册工具名（与 prompt 二选一；执行时走完整闸门）",
    )
    args: Dict[str, Any] = Field(
        default_factory=dict,
        description="tool 型任务的入参（须 JSON 可序列化，≤8KB）",
    )
    deliver: bool = Field(
        default=True,
        description="任务完成后是否投递站内 user_notification 通知",
    )


class cron_list(BaseModel):
    """``cron_list`` 入参（无参：列出全部任务）。"""


class cron_delete(BaseModel):
    """``cron_delete`` 入参（类名即工具名：schema title 契约）。"""

    job_id: str = Field(..., description="要删除的任务 id（cron_list / cron_create 返回）")


class cron_run(BaseModel):
    """``cron_run`` 入参（类名即工具名：schema title 契约）。"""

    job_id: str = Field(..., description="要立即执行一次的任务 id")


def build_cron_create_spec() -> ToolSpec:
    """构造 ``cron_create`` 声明（L1：持久化配置级副作用，禁 auto-retry）。"""
    return ToolSpec(
        name="cron_create",
        risk_level="L1",  # L1 ⇒ MEDIUM ⇒ HUMAN_REVIEW_ENABLED 时须人工批准
        description=(
            "创建一个 cron 定时任务（持久化，重启后仍按计划执行；需后台调度开启）。\n"
            "何时用：用户要「每天/每小时定时做 X」——X 是一段提示词（agent 型，"
            "到点跑一轮模型任务）或一个已注册工具（tool 型，到点经执行器跑）。\n"
            "何时不能用：一次性立即执行用 cron_run；取消定时用 cron_delete；"
            "当前对话内直接回答能解决的不要建定时任务。\n"
            "注意：表达式按 **UTC** 计；prompt 与 tool 必须二选一；"
            "删除/重复创建同一 name 不会自动去重——先 cron_list 核对。\n"
            "结果：返回 job_id 与首个 next_run_at，可经 cron_list 跟踪。"
        ),
        parameters=cron_create.model_json_schema(),
        side_effect=True,  # 持久化新任务 = 配置级副作用；禁止 auto-retry
        capabilities=("control",),
        timeout_ms=5_000,
    )


def build_cron_list_spec() -> ToolSpec:
    """构造 ``cron_list`` 声明（L0 只读）。"""
    return ToolSpec(
        name="cron_list",
        risk_level="L0",
        description=(
            "列出全部 cron 定时任务及其状态（id、名称、表达式、类型、"
            "下次执行时间 UTC、最近一次结果、是否启用）。\n"
            "何时用：用户问「有哪些定时任务/下次什么时候跑」，或在 "
            "cron_delete/cron_run 前核对 job_id。\n"
            "何时不能用：需要改任务（无 update——删了重建）或立即执行（cron_run）。"
        ),
        parameters=cron_list.model_json_schema(),
        side_effect=False,  # 纯读
        capabilities=("control",),
        timeout_ms=5_000,
    )


def build_cron_delete_spec() -> ToolSpec:
    """构造 ``cron_delete`` 声明（L1：删除持久化任务，禁 auto-retry）。"""
    return ToolSpec(
        name="cron_delete",
        risk_level="L1",
        description=(
            "删除一个 cron 定时任务（立即停止后续调度；不可恢复）。\n"
            "何时用：用户要取消/停止某个定时任务。\n"
            "何时不能用：只是想暂停（无 pause——删除后按需 cron_create 重建）；"
            "或不确定 id（先 cron_list 核对，删除不可撤销）。\n"
            "结果：返回被删任务的快照；id 不存在会诚实失败。"
        ),
        parameters=cron_delete.model_json_schema(),
        side_effect=True,  # 删除持久化任务 = 配置级副作用；禁止 auto-retry
        capabilities=("control",),
        timeout_ms=5_000,
    )


def build_cron_run_spec() -> ToolSpec:
    """构造 ``cron_run`` 声明（L1：立即触发执行，禁 auto-retry）。

    ``timeout_ms=130_000`` > ``CRON_JOB_TIMEOUT_S=120``（执行半 per-job 超时）
    ——工具自身的等待上限必须给足整轮执行时间，p58 有漂移守卫。
    """
    return ToolSpec(
        name="cron_run",
        risk_level="L1",
        description=(
            "立即执行一次指定 cron 任务（不等计划时间；正常调度节奏不变——"
            "下次仍按表达式走）。\n"
            "何时用：用户想「现在就跑一遍」验证定时任务，或临时补跑一次。\n"
            "何时不能用：任务正在执行中（会诚实失败，避免并发双跑）；"
            "或只是想看状态（cron_list）。\n"
            "结果：返回本轮执行摘要与新的 next_run_at；失败会进事故单（incident）。"
        ),
        parameters=cron_run.model_json_schema(),
        side_effect=True,  # 触发真实执行 = 副作用；禁止 auto-retry（防双跑）
        capabilities=("control",),
        timeout_ms=130_000,  # ≥ CRON_JOB_TIMEOUT_S(120s) + 余量（p58 漂移守卫）
    )
