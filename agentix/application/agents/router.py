"""
Query routing module - intent classification and dispatch.
"""

import re
from typing import Any, Dict, Literal, Optional, Set, Tuple

from langchain_core.runnables import RunnableConfig
from loguru import logger

from agentix.orchestration.langgraph.lg_states import (
    AgentState,
    CodeRouter,
    CodeSingleLayerRouter,
    Router,
    SingleLayerRouter,
    TaskCodeRouter,
    TaskCodeSingleLayerRouter,
    TaskRouter,
    TaskSingleLayerRouter,
)
from agentix.application.agents.llm_factory import get_llm_client
from agentix.application.agents.lg_prompts import (
    CODE_EXEC_ROUTE_BLOCK,
    ROUTER_SYSTEM_PROMPT,
    SINGLE_LAYER_ROUTE_BLOCK,
    TASK_EXEC_ROUTE_BLOCK,
)
from agentix.config import ablation, settings
from agentix.observability.metrics import record_llm_calls_saved, record_routing_cache
from agentix.observability.spans import record_route_decision, span


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
# 只收**确定性极高**的几类；其余一律照常走 LLM 分类，不做任何猜测。
# T8 扩面边界：只扩**落到 general-query 的助理元问题**（寒暄/致谢/能力/身份的
# 变体）。kb-query / graphrag-query / text2sql-query 那几类需要检索或查库，
# 误判代价高，不在扩面范围。
# ===========================================================================

# 寒暄（整句须为此类短句——任一多余内容即不命中，宁可走 LLM）
_CHATTER_EXACT = re.compile(
    r"^(?:你好|您好|hi|hello|hey|嗨|哈喽|早上好|早安|中午好|下午好|晚上好|晚安"
    r"|在吗|在么|在不在|你是谁|你叫什么|介绍(?:一下)?(?:你)?(?:自己)?|你能(?:做|干)(?:什么|啥)"
    r"|谢谢|多谢|感谢|再见|拜拜|拜拜了|好的?|嗯+|哦+|ok+|okay|收到"
    r")[!！。.~～,，、\s]*$",
    re.IGNORECASE,
)

# 助理能力元问题（"你会干什么 / 你有哪些功能"一类）。确定性来自**句式封闭**：
# 必须整句以"你/您"打头、随后就是"能做什么"这类固定骨架、且到尾为止——
# 任何实质内容（"你有能力处理这个案子吗"）都会让 fullmatch 失败，落回 LLM。
_CAPABILITY_META = re.compile(
    r"^(?:你|您)(?:都|还|到底|究竟)?"
    r"(?:"
    r"有(?:哪些|什么|啥)(?:功能|能力|本事|用法)"
    r"|会(?:做|干|用)?(?:什么|啥|哪些)"
    r"|(?:能|可)(?:以)?(?:帮我|帮忙)?(?:做|干)(?:什么|啥|哪些)"
    r"|支持(?:哪些|什么|啥)(?:功能|能力)"
    r"|的(?:功能|能力|作用)(?:有(?:哪些|什么|啥))?"
    r")"
    r"[!！。.?？~～,，、\s]*$",
    re.IGNORECASE,
)

# 助理身份元问题（"你是 AI 吗 / 你是什么模型"）。确定性来自**身份词封闭枚举**：
# 只有整句等于"你(是) <身份词> (吗)"才命中；"你是律师吗"这类会引出实质法律
# 咨询的问法不在枚举内，一律落回 LLM。
_IDENTITY_META = re.compile(
    r"^(?:你|您)(?:到底|究竟)?(?:是|叫)?"
    r"(?:"
    r"(?:一个|一台|一名|一款)?"
    r"(?:ai|人工智能|人工智能助手|机器人|大模型|模型|语言模型|智能助手|助手|程序|bot|gpt)"
    r"|(?:哪个|什么|哪家|谁)(?:大模型|模型|ai|公司|团队)"
    r")(?:吗|么|呢|呀)?"
    r"[!！。.?？~～,，、\s]*$",
    re.IGNORECASE,
)

# 收尾确认/道谢元问题（"我明白了 / 谢谢你的解答 / 辛苦了"）。确定性来自
# **整句封闭**：多一个字（"明白了，那第264条呢"）就不再命中——这类"确认 + 追问"
# 依然要经过分类器才知道该去检索还是直答。
_ACK_META = re.compile(
    r"^(?:"
    r"我?(?:明白了?|懂了|知道了|了解了?|清楚了)"
    r"|收到(?:了)?(?:谢谢|多谢|感谢)?"
    r"|没问题|不用了|不客气|辛苦了|麻烦你了?"
    r"|(?:太|非常)?(?:谢谢|多谢|感谢)(?:你|您)?(?:了|啦)?(?:的)?(?:解答|回答|帮助|支持|耐心解答)?"
    r")"
    r"[!！。.?？~～,，、\s]*$",
    re.IGNORECASE,
)

#: fast-path 命中的 reason **封闭枚举（单源）**：``llm_calls_saved_total{reason}``
#: 的取值由这里定义，``agentix/observability/metrics.py`` 的 ``LLM_CALLS_SAVED``
#: 注释与用例都引用本常量——**不许在别处再抄一份 reason 名单**（约定 6 ④）。
FASTPATH_REASONS: frozenset[str] = frozenset(
    {"chit_chat", "statute_direct", "capability_meta", "identity_meta", "ack_meta"}
)

# 数据条目引用（第X条[之X]）
_STATUTE_REF = re.compile(r"第[零一二三四五六七八九十百千万两〇0-9]+条(?:之[零一二三四五六七八九十0-9]+)?")

# 一旦出现即放弃 statute fast-path——需要推理/综合的问题绝不短路
_REASONING_MARKERS = (
    "为什么", "为何", "如何", "怎么", "区别", "比较", "案例", "分析", "意见",
    "建议", "适用", "认定", "竞合", "论证", "结合", "评价", "判断", "关系",
    "情形", "情况", "分别", "还是", "以及", "和", "与",
)

# 本函数的 fastpath 规则与测试侧样本同源维护（约定 6 ④：同一个判定不许两个源）。封闭
# 句式族的逐字集在 ``tests/test_p173_routing_cache.py`` 的 ``_FASTPATH_EXPANSION_HITS``
# （应短路）与 ``_FASTPATH_EXPANSION_NEAR_MISSES``（近似但不该短路）：改本函数任一规则，
# 须同批改这两份样本；反之改样本也要回看规则——否则规则与用例各说一套。用符号名而非行号
# 锚点，避免本文件位移把注释钉错位置（防腐规则）。
def _fastpath_router(question: str) -> Optional[Tuple[str, Router]]:
    """高置信确定性规则分类；命中返回 ``(reason, router)``，否则 None（照走 LLM）。

    - ``chit_chat``（寒暄/身份/致谢/确认）→ ``general-query``；
    - ``statute_direct``（短句单条数据条目直取，无任何推理词）→ ``graphrag-query``
      （与既有 ``_heuristic_router`` 数据条目关键词映射同向）；
    - ``capability_meta``（"你会做什么 / 你有哪些功能"）→ ``general-query``；
      （本行是句式族示意、不是逐字样本：封闭句式族的逐字集在 ``tests/test_p173_routing_cache.py``
      的 ``_FASTPATH_EXPANSION_HITS``，改规则须同批改样本，否则规则与用例各说一套。）
    - ``identity_meta``（"你是 AI 吗 / 你是什么模型"）→ ``general-query``；
    - ``ack_meta``（"我明白了 / 谢谢你的解答 / 辛苦了"）→ ``general-query``。

    每条规则都要求**整句**落在封闭句式内（``fullmatch`` 语义），因此"元问题 +
    实质内容"的混合句一律不命中。reason 词汇由 :data:`FASTPATH_REASONS` 单源定义。
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

    # T8 扩面：只新增**落到 general-query** 的助理元问题规则（见段落头注释）。
    for reason, pattern, logic in (
        (
            "capability_meta",
            _CAPABILITY_META,
            "fastpath: assistant capability meta (deterministic, LLM classifier skipped)",
        ),
        (
            "identity_meta",
            _IDENTITY_META,
            "fastpath: assistant identity meta (deterministic, LLM classifier skipped)",
        ),
        (
            "ack_meta",
            _ACK_META,
            "fastpath: acknowledgement meta (deterministic, LLM classifier skipped)",
        ),
    ):
        if pattern.fullmatch(text):
            return (reason, Router(type="general-query", logic=logic, question=text))

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


def _record_router_fallback(reason: str) -> None:
    """路由兜底计数（R-P3）：把"静默兜底"变成可归因指标。

    fail-open（工程约定 2）：指标子系统出错只留痕，绝不改路由结果——
    这条路径本身已经是降级路径，再抛异常就等于把降级变成故障。
    """
    try:
        from agentix.observability.metrics import record_router_fallback

        record_router_fallback(reason)
    except Exception:  # noqa: BLE001 - 指标降级：只留痕，绝不打断路由
        logger.debug("router fallback counter failed (non-fatal)")


def _router_class(task_exec_on: bool, code_exec_on: bool, single_layer_on: bool):
    """按 flag 组合选结构化输出 schema：单层闸关⇒与现网**同一批四类**。

    为什么用四个类而不是"运行时给 Literal 加值"：``Literal`` 是静态类型，
    结构化输出的 schema 在 ``with_structured_output(cls)`` 那一刻就按 cls 固化
    了——基类 ``Router`` 的 ``type`` 字面量若被放宽，基线消费者（任何按
    ``router.type`` 分派的既有代码）都要跟着重新审一遍。改用子类 ⇒ 基类字面量
    一个字不动，flag-off 时生产链路仍只构造基类 ``Router``（逐字节等价）。

    T30-2（单层路由）同理：KB 检索通道参数也做成**子类**（
    ``SingleLayerRouter`` 等四个，与 task/code 两闸的四种组合一一对应）。
    往基类加字段会让 flag-off 的 JSON schema 也变，等价性当场就破。
    """
    if single_layer_on:
        if task_exec_on and code_exec_on:
            return TaskCodeSingleLayerRouter
        if task_exec_on:
            return TaskSingleLayerRouter
        if code_exec_on:
            return CodeSingleLayerRouter
        return SingleLayerRouter
    if task_exec_on and code_exec_on:
        return TaskCodeRouter
    if task_exec_on:
        return TaskRouter
    if code_exec_on:
        return CodeRouter
    return Router


def _exec_flags() -> Tuple[bool, bool, bool]:
    """P1（任务执行）/ P2（代码执行）/ T30（单层路由）三个业务开关的当前取值。

    单独提出来是因为现在有**三处判定共用**这一个来源——结构化输出 schema、
    路由提示词、以及缓存换代令牌（``routing_cache.generation_token``）。
    各写一份 ``bool(getattr(settings, ...))`` 就是"同一个判定两个源"（约定 6 ④）。
    """
    return (
        bool(getattr(settings, "TASK_EXEC_ENABLED", False)),
        bool(getattr(settings, "CODE_EXEC_ENABLED", False)),
        bool(getattr(settings, "ROUTING_SINGLE_LAYER_ENABLED", False)),
    )


def _router_prompt_text(task_exec_on: bool, code_exec_on: bool, single_layer_on: bool) -> str:
    """路由提示词**实际生效文本**：基座 + flag-on 才拼入的支路段。

    flag-off 三段都不拼 ⇒ 与基线逐字节相同。缓存换代令牌取的就是本函数的返回值
    ——提示词变了（含"某段被拼入/移除"）就必须换代，否则旧结论会跨提示词版本复用。
    """
    prompt = ROUTER_SYSTEM_PROMPT
    if task_exec_on:
        prompt += TASK_EXEC_ROUTE_BLOCK
    if code_exec_on:
        prompt += CODE_EXEC_ROUTE_BLOCK
    if single_layer_on:
        prompt += SINGLE_LAYER_ROUTE_BLOCK
    return prompt


async def _lookup_routing_cache(question_text: str) -> Tuple[Optional[Router], Optional[str]]:
    """查路由结果缓存（T8，``ROUTING_CACHE_ENABLED`` 判真后才会被调用）。

    返回 ``(router, generation)`` 二元组：第一枚是命中并用**当前请求原文重建**的
    ``Router``（未命中或任何异常为 ``None``，让主链路照常走 LLM 分类）；第二枚是
    **本次算出的换代令牌**——查侧在 ``try`` 里算出后传给写侧 ``_store_routing_cache``
    复用，一次请求只算一枚令牌。``except`` 臂带 ``None``
    （没算出来就别让人复用），miss 臂与重建失败臂都带上已算出的那枚。

    fail-open（约定 2）：令牌计算、读缓存、重建三处任一抛异常都只 ``warning``
    留痕并落 ``routing_cache_total{result="error"}``，绝不让一次问答失败——缓存
    是加速器，不是链路上的新故障点。

    命中样本必须可对账（任务卡风险行）：用既有 ``span()`` 落一步
    ``route.cache_hit``（``cached=True`` + 有界 ``route_type``），trajectory 因此
    能证明"这次真的没调模型"；决策属性照常由 ``record_route_decision`` 单点写入
    （与 LLM 路径同一个 helper、同一个键，不另立第二源）。

    T30-2 的**已知能力缺口**（本批刻意不补）：缓存值只有 ``_CACHED_FIELDS`` 四个
    基线字段，**不含** ``kb_route``/``kb_tools``——扩它会改 T8 的缓存语义，不在本批
    边界内。后果是可预期的：同时开 ``ROUTING_CACHE_ENABLED`` 时，命中请求拿不到单层
    参数，KB 子图内的 A3 检索路由**照常自己调一次模型**（跳数回到基线，不会算错，
    只是没省）。因此命中路径重建出的 router 上那两个字段恒为缺省值。
    """
    # 惰性 import 是本任务 flag-off 等价性的实现手段之一：flag 判真之前
    # 缓存模块连加载都不发生（用例读 sys.modules 钉死这条）。
    from agentix.application.agents import routing_cache

    try:
        generation = routing_cache.generation_token()
        cached_value = routing_cache.get_routing_cache().get(
            question_text, generation=generation
        )
    except Exception as exc:  # noqa: BLE001 - 缓存降级：当作未命中，主链路照走 LLM
        logger.warning("routing cache lookup failed (degraded to miss): {}", exc)
        record_routing_cache("error")
        return None, None

    if cached_value is None:
        record_routing_cache("miss")
        return None, generation

    try:
        router_cls = _router_class(*_exec_flags())
        cached_router = router_cls(
            type=cached_value.get("type"),
            logic=cached_value.get("logic") or "",
            question=question_text,
            decision=cached_value.get("decision"),
            confidence=cached_value.get("confidence"),
        )
    except Exception as exc:  # noqa: BLE001 - 重建失败同样降级为未命中
        logger.warning("routing cache entry rebuild failed (degraded to miss): {}", exc)
        record_routing_cache("error")
        return None, generation

    record_routing_cache("hit")
    record_route_decision(cached_router.type)
    async with span("route.cache_hit", cached=True, route_type=cached_router.type):
        logger.info(
            "routing cache hit: skip LLM intent classifier -> {}", cached_router.type
        )
    return cached_router, generation


def _store_routing_cache(
    question_text: str, router_obj: Router, generation: Optional[str] = None
) -> None:
    """把**成功**分类结果写进缓存（T8）；写失败只留痕，不影响本次路由结果。

    只在 LLM 分类链路拿到白名单内的最终结果后调用：兜底出口（LLM 抛错 /
    非法类型）刻意不写——把"这一次没算出来"缓存下来，只会把同一失败复制给
    后续同问句。

    ``generation`` 允许由调用方（同一请求的查侧）传进来复用：一次请求只该算
    一枚换代令牌。默认 ``None`` 保住"独立调用"
    这一形态——拿不到时才自算兜底。传入的那一枚是**查侧那一刻**算出的令牌，
    条目因此按查侧那一代落键而非按写的那一刻的代落键；运行期的安全前提见写侧
    调用点注释（此处不复制论证）。
    """
    from agentix.application.agents import routing_cache

    try:
        routing_cache.get_routing_cache().put(
            question_text,
            router_obj,
            generation=(
                generation if generation is not None else routing_cache.generation_token()
            ),
        )
    except Exception as exc:  # noqa: BLE001 - 写缓存失败：降级为不缓存，主链路不受影响
        logger.warning("routing cache store failed (skip caching this round): {}", exc)
        record_routing_cache("error")


async def analyze_and_route_query(state: AgentState, *, config: RunnableConfig) -> dict:
    """Analyze the user's query and determine the appropriate routing."""
    # 评测专用（EVAL_FORCE_GENERAL_QUERY=true）：通识 MCQ 题集不经过意图分类
    # 与 KB/KG guardrails，直接落 general-query 由基座 LLM 作答。默认关 ⇒
    # 下方路由链逐位等价；开时零 LLM 分类调用。
    if settings.EVAL_FORCE_GENERAL_QUERY:
        question_text = state.messages[-1].content if state.messages else ""
        logger.info("eval[force_general_query]: skip intent classification")
        return {
            "router": Router(
                type="general-query",
                logic="eval: forced general-query (MCQ benchmark)",
                question=question_text,
            )
        }

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

    # T8：路由结果跨请求缓存（ROUTING_CACHE_ENABLED，默认关）。**位置固定在
    # 消融分支与 fast-path 之后**：消融是治理层的整体绕过、fast-path 是零成本
    # 确定性命中，两者都不该被缓存挡在前面（判定也不许复制第二份）。
    # **只服务单轮请求**：键只覆盖末句，而分类器输入是 ``[system] + state.messages``
    # （含窗口历史）——两段历史不同、末句相同的请求共用一条结论，会把上一轮的
    # ``logic`` 拼进这一轮的 system prompt。历史窗口 >1 条时既不查也不写
    # （只加查不加写，多轮请求仍会把缓存写脏，后面的单轮请求照样命中它）。
    # flag-off ⇒ 不 import 缓存模块、不读不写、不打日志、不记指标（逐位等价）。
    # cache_generation 先声明：flag-off 与多轮路径都碰不到缓存，但写侧那一跳
    # 引用它，缺了声明就是 NameError（None 形态 = 写侧自算兜底）。
    cache_generation: Optional[str] = None
    if len(state.messages) == 1 and bool(getattr(settings, "ROUTING_CACHE_ENABLED", False)):
        cached_router, cache_generation = await _lookup_routing_cache(question_text)
        if cached_router is not None:
            return {"router": cached_router}

    # P0-2(b)：显式绑定 router 角色 → 低成本/确定性 profile（temperature=0.0,
    # timeout=10s, max_retries=1）。调用点传 temperature=0.7 会落到 profile 温度
    # （llm_factory 的既有语义），即路由用 0.0，属预期行为。
    model = get_llm_client(temperature=0.7, tags=["router"], role="router")

    # P1（任务执行）/ P2（代码执行）/ T30（单层路由）：flag-on 才把对应段拼进路由
    # 提示 + 换结构化输出 schema +（P1/P2）放宽白名单；flag-off 三处均与基线逐字节
    # 相同（三段提示词都不进入任何提示词，router_cls 仍是基类 Router，allowed_types
    # 仍是 7 项基线）。三个判定各只有一个源（``_exec_flags`` / ``_router_class`` /
    # ``_router_prompt_text``），缓存换代令牌复用同一组函数。
    task_exec_on, code_exec_on, single_layer_on = _exec_flags()
    router_cls = _router_class(task_exec_on, code_exec_on, single_layer_on)
    router_prompt = _router_prompt_text(task_exec_on, code_exec_on, single_layer_on)
    allowed_types: Set[str] = set(ALLOWED_TYPES)
    if task_exec_on:
        allowed_types.add("task-query")
    if code_exec_on:
        allowed_types.add("code-query")

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
        logger.warning("Router LLM failed: {}. Falling back to KB query.", exc)
        _record_router_fallback("exception")
        return {"router": fallback_router}

    # 越域 kb_route 在此抛 ValidationError 使节点失败（G-2，见规划 §8.2 表 M）：刻意不做回退重写，与 type 字段越域的既有口径一致（fail-closed）
    response = raw_response if isinstance(raw_response, Router) else router_cls.model_validate(raw_response)
    router_type = response.type
    logic = response.logic or ""

    if not router_type or router_type not in allowed_types:
        logger.warning("Router returned invalid type `{}`; applying heuristic fallback.", router_type)
        _record_router_fallback("empty" if not router_type else "invalid_type")
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

    sanitized_kwargs: Dict[str, Any] = {
        "type": router_type,
        "logic": logic,
        "question": response.question or question_text,
        "decision": response.decision,
        "confidence": response.confidence,
        "reasoning": response.reasoning,
    }
    if single_layer_on:
        # T30-2 A1 产参：KB 检索通道参数必须**一并带出去**，否则参数在最后一跳被
        # 丢掉，A3 只能自己再问一次模型（单层化就只剩"多一段提示词"）。
        # 显式取值，不靠基类 ``extra="allow"`` 的透传：透传只在模型真的回传多余键时
        # 才成立，而且会把"这次没给参数"与"参数被丢在半路"混成同一种形态——那是
        # 静默失败，不是降级。
        # 兜底出口（异常 / 白名单外 / fast-path / 缓存命中）**一律不产这两个字段**：
        # 那是有意的降级，保持返回无参数的 Router，A3 照常自调模型（跳数回到基线），
        # **不伪造默认通道**。
        sanitized_kwargs["kb_route"] = getattr(response, "kb_route", None)
        sanitized_kwargs["kb_tools"] = list(getattr(response, "kb_tools", None) or [])

    sanitized_router = router_cls(**sanitized_kwargs)

    # PhaseK 任务 4：路由决策值上 span 的**两个落点之一**——白名单校验通过后的最终
    # route（有界枚举，约定 3；理由文本不落）。缓存命中分支也调本 helper：同在函数体级、
    # 先于 route.cache_hit 子 span 打开，两处同层级落在当前环境 span 上。
    # 两处同走 record_route_decision 单源，不另立第二份判据。兜底出口另有
    # _record_router_fallback 指标归因；纯 Python 分派的 route_query() 不调（它不返回
    # route_type）。fail-open 由 helper 自身保证（无 span/异常 ⇒ no-op，约定 2）。
    record_route_decision(sanitized_router.type)

    # T8：**成功拿到白名单内结果**才写缓存（兜底出口刻意不写，见 _store_routing_cache）。
    # 位置在决策属性之后：flag-off 时这一行只是判一个 settings 布尔，不产生任何副作用。
    # 与查缓存同一判据（单轮才写）：多轮请求写进来的是「按末句算出的结论」，
    # 后面的单轮同问句会命中它——写侧漏判等于把脏条目留给下一个请求。
    if len(state.messages) == 1 and bool(getattr(settings, "ROUTING_CACHE_ENABLED", False)):
        # 复用查侧令牌⇒一次请求只算一枚换代令牌；写侧拿不到（None）时才自算兜底。
        # 落代语义相对改前变了：改前按「写的那一刻」的代落键，本行按「本次请求查的那一刻」的代落键。
        # 只有请求窗口内令牌的某枚输入项被改写时两者才不同——那种情况下本行写法可能把「按中途新代配置算出的
        # 结论」挂到查侧那一代的键上，代别若在 TTL 内翻回该代，就可能产生一次错代命中
        # （改前写法在该场景只吃一次普通未命中，严格更安全）。
        # 成立前提：当前生产形态下代别在请求内不可变——令牌的全部输入项取自 settings 直读、既有路由判定（提示词文本与 router 类名，经 `_exec_flags` / `_router_prompt_text` / `_router_class` 取，不另立源）与
        # ablation（其设计前提是进程环境不变），仓内无运行期改写点；故该反例今天不可达，属已知前提
        # 而非已被证明的性质。档位路由的输入值已并入令牌（段数增加，前提不变：仍是 settings 直读 + 既有判定 + ablation）；
        # 若将来引入运行期热改配置，本行写法需重新评估。
        _store_routing_cache(question_text, sanitized_router, cache_generation)

    logger.info("Analyze user query type completed, result: {}", sanitized_router)
    return {"router": sanitized_router}


def route_query(state: AgentState) -> Literal[
    "respond_to_general_query", "get_additional_info", "create_research_plan",
    "create_image_query", "create_file_query", "create_kb_query",
    "execute_task", "code_agent",
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
    elif _type == "code-query":
        # P2（代码执行）：flag-on → code_agent 节点；flag-off 为防御性降级——
        # 正常链路 flag-off 时 with_structured_output 不会产出 code-query
        # （字面量校验在 analyze 处把关），此处兜底回 KB 检索并留痕。
        if bool(getattr(settings, "CODE_EXEC_ENABLED", False)):
            logger.info("route: code-query -> code_agent (CODE_EXEC_ENABLED)")
            return "code_agent"
        logger.warning("code-query received while CODE_EXEC_ENABLED=false; falling back to create_kb_query")
        return "create_kb_query"
    else:
        raise ValueError(f"Unknown router type {_type}")
