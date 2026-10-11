from typing import Any, Callable, Coroutine, Dict
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables.base import Runnable
from agentix.config import settings
from agentix.infrastructure.core.logger import get_logger

# 获取日志记录器
logger = get_logger(service="planner_node")

from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.models import Task
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.planner.models import PlannerOutput
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.planner.prompts import create_planner_prompt_template
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.planner import unified as planner_unified
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.state import InputState


# 定义planner prompt
planner_prompt = create_planner_prompt_template()


async def _shadow_unified_side(question: str) -> tuple[list[str], str | None]:
    """影子侧（统一 Planner）运行：异常只记报告，绝不影响主链路输出。"""
    try:
        tasks = await planner_unified.unified_decompose(question)
        return [t.question for t in tasks], None
    except Exception as exc:  # noqa: BLE001 — 影子侧异常只记不抛
        return [], f"{type(exc).__name__}: {exc}"


async def _shadow_in_graph_side(
    chain: Runnable[Dict[str, Any], Any], question: str
) -> tuple[list[str], str | None]:
    """影子侧（图内私有 LLM 链）运行：异常只记报告，绝不影响主链路输出。"""
    try:
        planner_output: PlannerOutput = await chain.ainvoke({"question": question})
        return [t.question for t in planner_output.tasks], None
    except Exception as exc:  # noqa: BLE001 — 影子侧异常只记不抛
        return [], f"{type(exc).__name__}: {exc}"


def create_planner_node(
    llm: BaseChatModel, ignore_node: bool = False, next_action: str = "tool_selection"
) -> Callable[[InputState], Coroutine[Any, Any, Dict[str, Any]]]:
    """
    Create a planner node to be used in a LangGraph workflow.

    A3（统一执行链·图内规划收敛）三分支：
    - 默认（双 flag 关）：图内私有 LLM 规划链，与基线逐位一致；
    - ``KG_PLANNER_UNIFIED_ENABLED``（切流）：统一 Planner 驱动任务分解
      （零 LLM、确定性），私有链不再执行；
    - ``KG_PLANNER_SHADOW_ENABLED``（影子对拍）：非驱动侧并行运行并落
      ``PlannerShadowReport``——只记不改，不一致/异常绝不影响输出。

    Parameters
    ----------
    llm : BaseChatModel
        The LLM used to process data.
    ignore_node : bool, optional
        Whether to ignore this node in the workflow, by default False

    Returns
    -------
    Callable[[InputState], OverallState]
        The LangGraph node.
    """

    # 创建planner chain
    planner_chain: Runnable[Dict[str, Any], Any] = (
        planner_prompt | llm.with_structured_output(PlannerOutput)
    )

    async def planner(state: InputState) -> Dict[str, Any]:
        """
        Break user query into chunks, if appropriate.
        """
        question = state.get("question", "")

        if ignore_node:
            # 与基线一致：忽略本节点 = 空分解 → 单任务兜底（不调任何 Planner）。
            tasks: list[Task] = []
        elif settings.KG_PLANNER_UNIFIED_ENABLED:
            # 切流：统一 Planner 驱动；图内私有链不再执行。
            tasks = await planner_unified.unified_decompose(question)
            if settings.KG_PLANNER_SHADOW_ENABLED:
                in_graph, error = await _shadow_in_graph_side(planner_chain, question)
                planner_unified.record_report(
                    question=question,
                    driver="unified",
                    in_graph_tasks=in_graph,
                    unified_tasks=[t.question for t in tasks],
                    error=error,
                )
        else:
            planner_output: PlannerOutput = await planner_chain.ainvoke(
                {"question": question}
            )
            tasks = planner_output.tasks
            if settings.KG_PLANNER_SHADOW_ENABLED:
                unified, error = await _shadow_unified_side(question)
                planner_unified.record_report(
                    question=question,
                    driver="in_graph",
                    in_graph_tasks=[t.question for t in tasks],
                    unified_tasks=unified,
                    error=error,
                )

        planner_task_decomposition = {
            "next_action": next_action,
            "tasks": tasks
            or [
                Task(
                    question=question,
                    parent_task=question,
                )
            ]
        }

        # 日志打印格式，分别打印每个任务
        logger.info(f"Total Sub Task: {len(planner_task_decomposition['tasks'])}")

        for i, task in enumerate(planner_task_decomposition['tasks']):
            logger.info(f"Sub Task[{i+1}]: {task.question}")

        return planner_task_decomposition

    return planner
