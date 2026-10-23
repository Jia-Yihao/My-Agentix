"""
Query routing module - intent classification and dispatch.
"""

import re
from typing import Literal, Optional, Set, Tuple

from langchain_core.runnables import RunnableConfig
from loguru import logger

from agentix.orchestration.langgraph.lg_states import AgentState, Router, TaskRouter
from agentix.application.agents.llm_factory import get_llm_client
from agentix.application.agents.lg_prompts import ROUTER_SYSTEM_PROMPT, TASK_EXEC_ROUTE_BLOCK
from agentix.config import ablation, settings
from agentix.observability.metrics import record_llm_calls_saved


def _ensure_router(router_obj, *, fallback_question: str = "") -> Router:
    """Convert any router structure to Router model with field access compatibility.

    注意这里**只在字段类型/字面量不合法时**才走 except 分支：pydantic 默认
    忽略多余键，所以 ``{"invalid": "data"}`` 其实会**校验成功**并得到全默认值。
    真正触发 except 的是「LLM 返回了不在 ``Router.type`` Literal 白名单里的
    路由类型」这类情况——它会静默退化成 ``kb-query``，也就是**每一次提问都
    被悄悄改成知识库检索**，且没有任何痕迹。

    因此保留 fallback（不能把整条链路打断），但**必须留日志**：否则
    「路由模型漂移」这个真实故障在运维侧完全不可见，只能靠用户反馈
    「问答答非所问」来发现。
    """
    if isinstance(router_obj, Router):
        return router_obj
    if isinstance(router_obj, dict):
        try:
            return Router.model_validate(router_obj)
        except Exception as exc:
            logger.warning(
                "Router payload failed validation, falling back to kb-query: "
                "err={} payload={!r}",
                exc,
                router_obj,
            )
    return Router(type="kb-query", logic="missing router", question=fallback_question)


def _extract_configurable(config) -> dict:
    """Extract configurable field from LangGraph RunnableConfig."""
    if not config:
        return {}
    if isinstance(config, dict):
        value = config.get("configurable", {})
        return value if isinstance(value, dict) else {}
    configurable = getattr(config, "configurable", None)
    if isinstance(configurable, dict):
        return configurable
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter("configurable", {})
            if isinstance(value, dict):
                return value
        except Exception:
            pass
    return {}


def _coerce_to_bool(value, *, default: bool = False) -> bool:
    """Best-effort conversion of dynamic configuration values to boolean."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _heuristic_router(question: str) -> Optional[Router]:
    """Fallback routing based on simple keyword heuristics."""
    if not question:
        return None

    lowered = question.lower()

    graphrag_keywords = [
        "法条", "怎么规定", "如何规定", "适用", "案由",
        "司法解释", "赔偿", "违约", "量刑", "处罚",
        "责任", "权利义务", "引用", "上位法",
    ]

    text2sql_keywords = ["统计", "多少", "总数", "数量", "排名"]

    if any(keyword in lowered for keyword in text2sql_keywords):
        return Router(
            type="text2sql-query",
            logic="keyword fallback: text2sql",
            question=question,
        )

    if any(keyword in lowered for keyword in graphrag_keywords):
        return Router(
            type="graphrag-query",
            logic="keyword fallback: graphrag",
            question=question,
        )

    return None


# ===========================================================================
# Phase H·W1：复杂度感知 fast-path（ROUTING_FASTPATH_ENABLED，默认关）
# 高置信确定性输入前置短路 ⇒ 跳过 LLM 意图分类器（零模型构造、零调用）。
# 只收**确定性极高**的两类；其余一律照常走 LLM 分类，不做任何猜测。
# ===========================================================================

# 寒暄（整句须为此类短句——任一多余内容即不命中，宁可走 LLM）
_CHATTER_EXACT = re.compile(
    r"^(?:你好|您好|hi|hello|hey|嗨|哈喽|早上好|早安|中午好|下午好|晚上好|晚安"
    r"|在吗|在么|在不在|你是谁|你叫什么|介绍(?:一下)?(?:你)?(?:自己)?|你能(?:做|干)(?:什么|啥)"
    r"|谢谢|多谢|感谢|再见|拜拜|拜拜了|好的?|嗯+|哦+|ok+|okay|收到"
    r")[!！。.~～,，、\s]*$",
    re.IGNORECASE,
)

# 数据条目引用（第X条[之X]）
_STATUTE_REF = re.compile(r"第[零一二三四五六七八九十百千万两〇0-9]+条(?:之[零一二三四五六七八九十0-9]+)?")

# 一旦出现即放弃 statute fast-path——需要推理/综合的问题绝不短路
_REASONING_MARKERS = (
    "为什么", "为何", "如何", "怎么", "区别", "比较", "案例", "分析", "意见",
    "建议", "适用", "认定", "竞合", "论证", "结合", "评价", "判断", "关系",
    "情形", "情况", "分别", "还是", "以及", "和", "与",
)


def _fastpath_router(question: str) -> Optional[Tuple[str, Router]]:
    """高置信确定性规则分类；命中返回 ``(reason, router)``，否则 None（照走 LLM）。

    - ``chit_chat``（寒暄/身份/致谢/确认）→ ``general-query``；
    - ``statute_direct``（短句单条数据条目直取，无任何推理词）→ ``graphrag-query``
      （与既有 ``_heuristic_router`` 数据条目关键词映射同向）。
    """
    if not question:
        return None
    text = question.strip()
    if not text or len(text) > 60:
        return None

    if _CHATTER_EXACT.fullmatch(text):
        return (
            "chit_chat",
            Router(
                type="general-query",
                logic="fastpath: chit-chat (deterministic, LLM classifier skipped)",
                question=text,
            ),
        )

    if _STATUTE_REF.search(text) and not any(m in text for m in _REASONING_MARKERS):
        return (
            "statute_direct",
            Router(
                type="graphrag-query",
                logic="fastpath: single statute direct (deterministic, LLM classifier skipped)",
                question=text,
            ),
        )

    return None


ALLOWED_TYPES: Set[str] = {
    "general-query",
    "additional-query",
    "kb-query",
    "graphrag-query",
    "image-query",
    "file-query",
    "text2sql-query",
}


async def analyze_and_route_query(state: AgentState, *, config: RunnableConfig) -> dict:
    """Analyze the user's query and determine the appropriate routing."""
    # 消融开关（AGENTIX_ABLATION_ROUTER_ENABLED=false）：
    # 彻底绕过意图分类——既不构造 router 角色 LLM，也不发分类请求，
    # 固定返回 kb-query。这才是变体 B_rag「固定向量检索（绕过路由）」的语义；
    # 若只在下游 route_query 改返回值，LLM 分类请求照发，变体名就名不副实。
    if not ablation.is_enabled("router_enabled"):
        question_text = state.messages[-1].content if state.messages else ""
        logger.info(
            "ablation[router_enabled=False]: skip intent classification, fixed kb-query"
        )
        return {
            "router": Router(
                type="kb-query",
                logic="ablation: router disabled (fixed kb-query)",
                question=question_text,
            )
        }

    # Phase H·W1 复杂度感知 fast-path（ROUTING_FASTPATH_ENABLED，默认关）：
    # 高置信确定性输入（寒暄 / 单条数据条目直取）前置短路——零模型构造、零 LLM
    # 分类调用（_boom 哨兵测试钉死），命中记 llm_calls_saved_total；flag-off
    # 或未命中 ⇒ 下方链路与基线逐位等价（question_text 为纯读，提前无副作用）。
    question_text = state.messages[-1].content if state.messages else ""
    if bool(getattr(settings, "ROUTING_FASTPATH_ENABLED", False)):
        fast_hit = _fastpath_router(question_text)
        if fast_hit is not None:
            fast_reason, fast_router = fast_hit
            record_llm_calls_saved(fast_reason)
            logger.info(
                "routing fast-path hit ({}): skip LLM intent classifier -> {}",
                fast_reason,
                fast_router.type,
            )
            return {"router": fast_router}

    # P0-2(b)：显式绑定 router 角色 → 低成本/确定性 profile（temperature=0.0,
    # timeout=10s, max_retries=1）。调用点传 temperature=0.7 会落到 profile 温度
    # （llm_factory 的既有语义），即路由用 0.0，属预期行为。
    model = get_llm_client(temperature=0.7, tags=["router"], role="router")

    # P1（任务执行）：flag-on 才把 task-query 段拼进路由提示 + 换结构化输出 schema
    # + 放宽白名单；flag-off 三处均与基线逐字节相同（TASK_EXEC_ROUTE_BLOCK 不进入
    # 任何提示词，router_cls 仍是基类 Router）。
    task_exec_on = bool(getattr(settings, "TASK_EXEC_ENABLED", False))
    router_cls = TaskRouter if task_exec_on else Router
    router_prompt = ROUTER_SYSTEM_PROMPT + TASK_EXEC_ROUTE_BLOCK if task_exec_on else ROUTER_SYSTEM_PROMPT
    allowed_types: Set[str] = ALLOWED_TYPES | {"task-query"} if task_exec_on else ALLOWED_TYPES

    messages = [{"role": "system", "content": router_prompt}] + state.messages
    logger.info("-----Analyze user query type-----")

    heuristic_router = _heuristic_router(question_text)
    fallback_router: Router = heuristic_router or Router(
        type="kb-query",
        logic="fallback: default to knowledge base routing",
        question=question_text,
    )

    try:
        raw_response = await model.with_structured_output(router_cls).ainvoke(messages)
    except Exception as exc:
        logger.warning("Router LLM failed: %s. Falling back to KB query.", exc)
        return {"router": fallback_router}

    response = raw_response if isinstance(raw_response, Router) else router_cls.model_validate(raw_response)
    router_type = response.type
    logic = response.logic or ""

    if not router_type or router_type not in allowed_types:
        logger.warning("Router returned invalid type `%s`; applying heuristic fallback.", router_type)
        heuristic_router = _heuristic_router(question_text)
        if heuristic_router:
            sanitized = heuristic_router
            if not sanitized.logic:
                sanitized.logic = logic or ""
            return {"router": sanitized}
        return {
            "router": Router(
                type="kb-query",
                logic=logic or "fallback: invalid router output",
                question=question_text,
            )
        }

    sanitized_router = router_cls(
        type=router_type,
        logic=logic,
        question=response.question or question_text,
        decision=response.decision,
        confidence=response.confidence,
        reasoning=response.reasoning,
    )

    logger.info("Analyze user query type completed, result: %s", sanitized_router)
    return {"router": sanitized_router}


def route_query(state: AgentState) -> Literal[
    "respond_to_general_query", "get_additional_info", "create_research_plan",
    "create_image_query", "create_file_query", "create_kb_query",
    "execute_task",
]:
    """Determine next step based on query classification."""
    router = _ensure_router(
        getattr(state, "router", None),
        fallback_question=state.messages[-1].content if state.messages else "",
    )
    state.router = router
    _type = router.type or "kb-query"

    if hasattr(state, "config") and state.config:
        cfg = state.config.get("configurable", {})
        if cfg.get("image_path"):
            logger.info("Detected image path, routing to image query")
            return "create_image_query"
        if cfg.get("file_path"):
            logger.info("Detected file path, routing to file query")
            return "create_file_query"

    # 消融开关（AGENTIX_ABLATION_ROUTER_ENABLED=false）：
    # 与 analyze_and_route_query 同源。上面的 image_path/file_path 是**调用方显式给定的
    # 事实**（不是模型的路由决策），因此优先于本开关；除此之外一律固定走向量检索通道。
    if not ablation.is_enabled("router_enabled"):
        logger.info("ablation[router_enabled=False]: fixed route -> create_kb_query")
        return "create_kb_query"

    if _type == "general-query":
        return "respond_to_general_query"
    elif _type == "additional-query":
        return "get_additional_info"
    elif _type in ("graphrag-query", "text2sql-query"):
        return "create_research_plan"
    elif _type == "image-query":
        return "create_image_query"
    elif _type == "file-query":
        return "create_file_query"
    elif _type == "kb-query":
        return "create_kb_query"
    elif _type == "task-query":
        # P1（任务执行）：flag-on → execute_task 节点；flag-off 为防御性降级——
        # 正常链路 flag-off 时 with_structured_output(Router) 不可能产出
        # task-query（字面量校验在 analyze 处就把关），此处兜底回 KB 检索并留痕。
        if bool(getattr(settings, "TASK_EXEC_ENABLED", False)):
            logger.info("route: task-query -> execute_task (TASK_EXEC_ENABLED)")
            return "execute_task"
        logger.warning("task-query received while TASK_EXEC_ENABLED=false; falling back to create_kb_query")
        return "create_kb_query"
    else:
        raise ValueError(f"Unknown router type {_type}")
