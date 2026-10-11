"""A3（统一执行链·图内规划收敛）——统一 Planner 适配与影子对拍报告（默认关）。

背景（方案 v2 §1「P0 规划重叠」实证）：KG 子图 planner 节点自带私有 LLM
规划链，与 Harness 统一 Planner（`agentix.agent.planner`）并行发明两套
Plan 模型。A3 的收敛路线是「影子对拍 → 切流」（v2 §8 风险对策：影子对拍
报告先行，golden 回归零抖动才切流）：

- 切流（``settings.KG_PLANNER_UNIFIED_ENABLED``）：planner 节点跳过私有
  LLM 链，改经 :func:`unified_decompose` 走统一 Planner——默认单步规划
  （`SingleStepPlanner`，零 LLM、确定性），每个 plan step 映射为一个图内
  ``Task``（step 为空时与图内链空回退同构：单任务兜底）。
- 影子对拍（``settings.KG_PLANNER_SHADOW_ENABLED``）：非驱动侧并行运行，
  结果落 :class:`PlannerShadowReport`（有界报告队列 + 结构化日志）。
  **报告只记不改**：不一致与影子侧异常都绝不影响主链路输出。

两 flag 均默认关；flag-off 时本模块零触达（节点走原图内链，逐位不变）。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional, Tuple

from agentix.policies.planner import SingleStepPlanner
from agentix.domain.agent.types import TaskSpec
from agentix.infrastructure.core.logger import get_logger

from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.models import Task

logger = get_logger(service="planner-shadow")

#: 报告队列上限（对齐 guardrails 计数器的有界纪律，防长跑泄漏）。
MAX_REPORTS = 200


@dataclass(frozen=True)
class PlannerShadowReport:
    """一次影子对拍的结构化报告。

    ``in_graph`` / ``unified`` 为两侧任务问题元组（已 strip 归一）；
    ``driver`` 标明哪一侧在驱动真实输出（``"in_graph"`` / ``"unified"``）；
    ``error`` 为**非驱动侧**异常（驱动侧异常按原语义直接抛出，不进报告）；
    ``agree`` = 无异常且两侧任务序列完全一致。
    """

    seq: int
    question: str
    driver: str
    in_graph: Tuple[str, ...]
    unified: Tuple[str, ...]
    agree: bool
    error: Optional[str] = None


#: 有界报告队列（进程内；测试经 get/clear 访问，生产只追加）。
_REPORTS: deque[PlannerShadowReport] = deque(maxlen=MAX_REPORTS)


def get_reports() -> Tuple[PlannerShadowReport, ...]:
    """返回当前全部影子对拍报告（按追加序）。"""
    return tuple(_REPORTS)


def clear_reports() -> None:
    """清空报告队列并把序号归一到 1（测试夹具用）。"""
    _REPORTS.clear()


def record_report(
    *,
    question: str,
    driver: str,
    in_graph_tasks: list[str],
    unified_tasks: list[str],
    error: Optional[str] = None,
) -> PlannerShadowReport:
    """落一份影子对拍报告并输出结构化日志（绝不抛出语义之外的副作用）。

    - 影子侧异常 → ``error`` 记录 + warning（对拍不可比，``agree=False``）；
    - 两侧不一致 → info（golden 回归看的就是这条）；
    - 一致 → info。
    """
    in_graph = tuple(t.strip() for t in in_graph_tasks)
    unified = tuple(t.strip() for t in unified_tasks)
    report = PlannerShadowReport(
        seq=(_REPORTS[-1].seq + 1) if _REPORTS else 1,
        question=question,
        driver=driver,
        in_graph=in_graph,
        unified=unified,
        agree=error is None and in_graph == unified,
        error=error,
    )
    _REPORTS.append(report)

    if error is not None:
        logger.warning(
            "planner shadow compare errored (non-driving side, non-fatal): {} driver={}",
            error,
            driver,
        )
    elif not report.agree:
        logger.info(
            "planner shadow mismatch: in_graph={} unified={} driver={}",
            len(in_graph),
            len(unified),
            driver,
        )
    else:
        logger.info("planner shadow agree: tasks={} driver={}", len(in_graph), driver)
    return report


def get_unified_planner() -> SingleStepPlanner:
    """统一 Planner 取用点（测试可 monkeypatch；未来 LLM 规划器在此换装）。

    现役与 `agent_loop_adapter` 生产口径一致：`SingleStepPlanner` 零 LLM、
    确定性回显——切流后分解退化为单任务（与图内链空回退同构），检索行为
    不引入新的不确定性，这正是「golden 零抖动」能成立的前提。
    """
    return SingleStepPlanner()


async def unified_decompose(question: str) -> list[Task]:
    """统一 Planner 分解：``Plan`` → 图内 ``Task`` 列表（每 plan step 一个任务单元）。

    ``plan.steps`` 为空时与图内私有链的空回退同构：单任务（原问题）兜底。
    """
    plan = await get_unified_planner().plan(TaskSpec(goal=question))
    if not plan.steps:
        return [Task(question=question, parent_task=question)]
    return [Task(question=question, parent_task=question) for _ in plan.steps]
