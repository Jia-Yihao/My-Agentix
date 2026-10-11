"""批次 10（§10.10.1）——KG 子图终态验证器适配节点（默认关）。

设计要点（详见方案文档 §10.10.1）：
1. 复用 ``validate_final_answer`` 的 **prompt 模板与响应模型**构成校验链，
   但**不复用**其 ``next_action`` 回跳语义（原节点 loop-back 到 text2cypher，
   主链路无此节点；该节点函数已于 2026-09 授权清理中**删除**，
   处置表见 ``tests/test_p24_deadcode_marking.py``）；
2. ``valid=True`` → 直通，steps 记 ``final_answer_validated``；
   ``valid=False`` → **降级放行**：答案原样下发（本节点不产出/不修改答案），
   steps 记 ``final_answer_validation_failed`` + warning 携带 follow_up_question；
   校验链**异常** → 同样放行，steps 记 ``final_answer_validation_error``。
   绝不因校验失败/异常扣下答案——校验器不能成为主链路单点故障；
3. ``settings.KG_FINAL_ANSWER_VALIDATION=False``（默认）时整个节点 no-op，
   steps 零增量，行为与未插节点时逐位一致；
4. **流式契约（硬性契约 1）**：本节点是内部编排调用，``ainvoke`` **源头不转发
   config**——否则其 token 会以 ``final_answer_guard`` 节点名冒泡。纵使如此，
   ``ChatService._STREAM_FORWARD_NODES`` 白名单也不含本节点名（双保险）。
5. **Phase H·W1 验证预算**：校验本身受三旋钮硬约束（``VERIFICATION_MAX_LLM_CALLS``
   按**本次答案**已尝试次数计、``VERIFICATION_TIMEOUT_MS`` 单次调用超时、
   ``VERIFICATION_TOKEN_BUDGET`` 按 count_tokens 粗估累计）；任一维度超预算
   ⇒ **fail-open 降级放行**（与既有校验失败降级同语义，steps 留痕），
   绝不因预算扣下答案。默认值（1 / 1000ms / 0）= 现行为除超时外逐位不变。
"""
import asyncio
from typing import Any, Callable, Coroutine, Dict

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables.base import Runnable
from langchain_neo4j import Neo4jGraph

from agentix.config import settings
from agentix.infrastructure.core.logger import get_logger

from .state import OverallState
from .utils.utils import retrieve_and_parse_schema_from_graph_for_prompts
from .validate_final_answer.models import ValidateFinalAnswerResponse
from .validate_final_answer.prompts import create_validate_final_answer_prompt_template

logger = get_logger(service="final-answer-guard")


def _estimate_tokens(text: str) -> int:
    """粗估 token（len//4，与上下文预算默认口径同源；不引入分词器依赖）。"""
    return max(1, len(text) // 4)


def create_final_answer_guard(
    llm: BaseChatModel, graph: Neo4jGraph
) -> Callable[[OverallState], Coroutine[Any, Any, Dict[str, Any]]]:
    """创建终态验证 guard 节点，插在 ``summarize → final_answer`` 之间。

    Parameters
    ----------
    llm : BaseChatModel
        用于结构化校验的 LLM（与工作流其余节点共用实例）。
    graph : Neo4jGraph
        图数据库包装（取 schema 供校验 prompt 使用）。
    """
    validate_chain: Runnable = (
        create_validate_final_answer_prompt_template()
        | llm.with_structured_output(ValidateFinalAnswerResponse)
    )
    # Phase H·W1：token 预算台账（guard 实例级；0 = 不限额默认恒放行）。
    token_spent = {"tokens": 0}

    async def final_answer_guard(state: OverallState) -> Dict[str, Any]:
        # 默认关：零行为变化（steps 零增量，不产生任何 LLM 调用）。
        if not settings.KG_FINAL_ANSWER_VALIDATION:
            return {}

        # -- Phase H·W1 预算 1/3：按本次答案的已尝试次数（state.steps 只读） --
        max_calls = int(getattr(settings, "VERIFICATION_MAX_LLM_CALLS", 1) or 0)
        if max_calls <= 0:
            return {"steps": ["final_answer_validation_budget"]}
        try:
            prior_attempts = sum(
                1
                for s in state.get("steps", list()) or []
                if isinstance(s, str) and s.startswith("final_answer_validat")
            )
        except Exception:  # noqa: BLE001 - 计数失败按未超预算处理（fail-open 校验）
            prior_attempts = 0
        if prior_attempts >= max_calls:
            logger.warning(
                "final answer validation budget hit ({} >= {}); degrade-pass",
                prior_attempts,
                max_calls,
            )
            return {"steps": ["final_answer_validation_budget"]}

        # -- Phase H·W1 预算 2/3：累计 token（粗估口径，0 = 不限额） --
        token_budget = int(getattr(settings, "VERIFICATION_TOKEN_BUDGET", 0) or 0)
        if token_budget > 0 and token_spent["tokens"] >= token_budget:
            logger.warning(
                "final answer validation token budget hit ({} >= {}); degrade-pass",
                token_spent["tokens"],
                token_budget,
            )
            return {"steps": ["final_answer_validation_token_budget"]}

        payload = {
            "question": state.get("question"),
            "answer": state.get("summary"),
            "schema": retrieve_and_parse_schema_from_graph_for_prompts(graph),
            "data": [
                cypher.get("records")
                for cypher in state.get("cyphers", list())
            ],
        }

        # -- Phase H·W1 预算 3/3：单次调用超时（0 = 不限时，与基线等价） --
        timeout_ms = int(getattr(settings, "VERIFICATION_TIMEOUT_MS", 1000) or 0)
        invocation = validate_chain.ainvoke(payload)
        if timeout_ms > 0:
            invocation = asyncio.wait_for(invocation, timeout=timeout_ms / 1000.0)

        # token 台账：失败/超时路径也记输入估算（调用已发生，成本已花）。
        input_cost = _estimate_tokens(str(payload))

        try:
            # 硬性契约 1：内部编排调用**源头不转发 config**（见模块 docstring 4）。
            response: ValidateFinalAnswerResponse = await invocation
        except asyncio.TimeoutError:  # noqa: UP041 - py3.10 下 wait_for 抛本类
            if token_budget > 0:
                token_spent["tokens"] += input_cost
            logger.warning(
                "final answer validation timed out after {}ms (degrade-pass, non-fatal)",
                timeout_ms,
            )
            return {"steps": ["final_answer_validation_timeout"]}
        except Exception as exc:  # noqa: BLE001 — 校验失败绝不阻断主链路
            if token_budget > 0:
                token_spent["tokens"] += input_cost
            logger.warning(
                "final answer validation errored (degrade-pass, non-fatal): {}", exc
            )
            return {"steps": ["final_answer_validation_error"]}

        if token_budget > 0:
            token_spent["tokens"] += input_cost + _estimate_tokens(str(response))

        if response.valid:
            return {"steps": ["final_answer_validated"]}

        logger.warning(
            "final answer validation failed (degrade-pass); follow_up_question={}",
            response.follow_up_question,
        )
        return {"steps": ["final_answer_validation_failed"]}

    return final_answer_guard
