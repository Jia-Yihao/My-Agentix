"""
LangGraph agent builder - backward-compatible re-export layer.

handler 与路由判定不再顶层 import 具体模块（§7.5 第③步，M3-3）：本模块内的
同名壳函数在**被调用时**才经 `agentix/orchestration/node_registry.py` 延迟解析
真身并原样转发（键名映射见本文件「延迟解析壳」区——节点名与 handler 函数名本就
不同，如节点 `code_agent` 的 handler 是 `create_code_query`）。签名与真身逐字
一致由 `tests/test_p180_builder_shells.py` 的运行时守卫钉住。纯 re-export 别名
（_heuristic_router / _extract_configurable / _coerce_to_bool /
AdditionalGuardrailsOutput）已删——全仓 0 消费者，判据源在各自真身模块。

`create_research_plan` 的真身已随 M3-4 迁到 `agentix/application/agents/research_handler.py`
（KG 面的具体实现不再由编排层 import），本模块只留它的第 10 个延迟解析壳；
`check_hallucinations` 仍留本文件——它是 DEAD-CODE(P2-5)｜保留·有意不接线的文档化未接线能力
（绊线与处置理由见 `tests/test_p24_deadcode_marking.py`）。

M3-5b 拆掉 `router._ensure_router` 这条反向边后，`route_query` 的条件边**注解取源**
在 import 期经注册表现取真身 ⇒ 本模块的 import 期要求装配已加载：不 import 装配根
（`agentix/main.py` / `agentix/application/agents/main.py`）而直接 import 本模块的
入口，必须自行先 `import agentix.application.wiring`。
"""

from typing import cast, get_type_hints, Any, Dict, List

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from loguru import logger

from agentix.config import ablation, settings
from agentix.observability.spans import record_resume_from_checkpoint
from agentix.observability.metrics import record_graph_resume
from agentix.orchestration.langgraph.graph_config import durability_kwargs, thread_config
from agentix.orchestration.langgraph.lg_states import AgentState, GradeHallucinations
from agentix.orchestration.langgraph.policies import (
    node_error_handler,
    node_timeout,
    readonly_retry_policy,
)
from agentix.application.agents.lg_prompts import (
    CHECK_HALLUCINATIONS,
)
from agentix.orchestration import capabilities, node_registry


# ---------------------------------------------------------------------------
# 延迟解析壳（M3-3，§7.5 第③步）
# ---------------------------------------------------------------------------
# 为什么是壳、而不是在 build_graph() 里 resolve：本模块在 **import 期**就执行
# `graph = build_graph(checkpointer)`（见文件末尾），建图期解析节点真身会把 10 个
# 实现一次性绑进节点，测试对 application 模块属性的 patch 就打不中（注册表的
# thunk 语义因此失效）。壳把解析点后移到**节点被调用时**：装配缺失则在首次执行点
# 响亮失败（fail-closed，绝不静默降级）。
# 注意（M3-5b 起）：**节点真身**仍在调用期解析，但下面 `route_query` 的**注解取源**
# 发生在 import 期（langgraph 在 `add_conditional_edges` 那一刻读 `Literal`），
# 它经注册表现取 ⇒ 本模块的 import 期就要求装配已加载；`evals/`、`bench/`、独立
# 脚本这类不 import 装配根的入口必须自行先 `import agentix.application.wiring`
# （经 chat_service 的入口由该模块的挂线位负责，顺序契约见 p180 的 P4 探针）。
#
# 键名映射（照 agentix/application/wiring.py 的登记，不是本模块发明的第二真源）：
# 9 个 handler 壳里只有 `create_code_query` 的**壳名 ≠ 注册键**——它解析的是
# `"code_agent"` 键（wiring 登记 `code_agent` → `code_handler.create_code_query`，
# 节点名与 handler 函数名本就不同，沿用改造前的映射）。其余壳的键与壳名逐字相同。
# 10 个壳里有 9 个的真身住 application（M3-3 的 8 个 + M3-4 迁入
# `agentix/application/agents/research_handler.py` 的 `create_research_plan`）；
# `route_query` 是同步壳，真身与其余 handler 同在 application。
#
# 签名壳的参数名/kind（含 keyword-only 的 `config`）必须与真身逐字一致——
# langgraph 的 config 注入按签名判定（去掉 `config` 形参则注入不到），节点名按
# `__name__` 推断。返回注解**刻意不抄**：抄一份就是第二份契约源（工程约定 6 ④），
# 签名一致性由 tests/test_p180_builder_shells.py 用
# inspect.signature 运行时对比兜底。


async def analyze_and_route_query(state: AgentState, *, config: RunnableConfig):
    """延迟解析壳：调用时解析 `analyze_and_route_query` 并原样转发。"""
    return await node_registry.resolve("analyze_and_route_query")(state, config=config)


def route_query(state: AgentState):
    """延迟解析壳（同步）：调用时解析 `route_query` 并原样转发。"""
    return node_registry.resolve("route_query")(state)


# 条件边的**静态枚举面**补注：langgraph 的 `BranchSpec.from_path` 按 path 函数返回
# 注解里的 `Literal` 推断分支终点集合；壳刻意不在源码里**书写**这份 Literal（抄一份
# 字面量 = 第二份契约源，工程约定 6 ④），而是在定义期把真源 `router.route_query`
# **求值后的注解对象**挂回壳上——值不在本文件落地，图的条件下拉枚举面与改造前逐位
# 一致（等价基准：`tests/test_p178_node_registry.py` 的 EXPECTED_GRAPH_EDGES，18 条边；
# 挂接失效时条件边塌成 `analyze_and_route_query → __end__`，该守卫当场变红，
# 另有 `tests/test_p180_builder_shells.py` 的 P5 探针直接断言出边集合不塌缩）。
# 必须挂**对象**而不是字符串：`get_type_hints(壳)` 以本模块 globals 求值，而本模块
# 没有 `Literal` 名字——字符串形态会 NameError 被 langgraph 静默吞掉（无声塌缩）。
# 真源自 M3-5b 起**经节点注册表现取**（拆掉 `router._ensure_router` 那条反向边）：
# wiring 登记的 provider 是 `lambda: router.route_query`，返回的就是那个函数对象
# 本身，而 `get_type_hints` 按函数自己的 `__globals__` 求值 ⇒ 拿到的 `Literal`
# 与改前逐字等价。代价是**本模块的 import 期就要求装配已加载**：未装配时
# `node_registry.resolve` 抛 `NodeNotRegistered`（带 `WIRING_HINT`）并原样抛在
# import 期——刻意不加 try/except、不做 `registered()` 兜底、不退回 import：
# 静默兜底等于把「无声塌缩」换成连一次响亮失败都不留下。直接 import 本模块的
# 入口（`bench/`、独立脚本）必须自行先挂线 `agentix.application.wiring`。
_route_query_ret = get_type_hints(node_registry.resolve("route_query")).get("return")
if _route_query_ret is not None:
    route_query.__annotations__["return"] = _route_query_ret
else:  # 本分支只留痕、不兜底（与上方「刻意不加 try/except、不做 registered() 兜底」同一口径）
    # 塌缩不静默：tests/test_p180_builder_shells.py 的 BRANCH_TARGETS 与 p178 EXPECTED_GRAPH_EDGES(18) 当场判红
    logger.warning("lg_builder: route_query 返回注解取不到 ⇒ 条件边 Literal 塌缩")
del _route_query_ret


async def respond_to_general_query(state: AgentState, *, config: RunnableConfig):
    """延迟解析壳：调用时解析 `respond_to_general_query` 并原样转发。"""
    return await node_registry.resolve("respond_to_general_query")(state, config=config)


async def create_kb_query(state: AgentState, *, config: RunnableConfig):
    """延迟解析壳：调用时解析 `create_kb_query` 并原样转发。"""
    return await node_registry.resolve("create_kb_query")(state, config=config)


async def get_additional_info(state: AgentState, *, config: RunnableConfig):
    """延迟解析壳：调用时解析 `get_additional_info` 并原样转发。"""
    return await node_registry.resolve("get_additional_info")(state, config=config)


async def create_image_query(state: AgentState, *, config: RunnableConfig):
    """延迟解析壳：调用时解析 `create_image_query` 并原样转发。"""
    return await node_registry.resolve("create_image_query")(state, config=config)


async def create_file_query(state: AgentState, *, config: RunnableConfig):
    """延迟解析壳：调用时解析 `create_file_query` 并原样转发。"""
    return await node_registry.resolve("create_file_query")(state, config=config)


async def execute_task(state: AgentState, *, config: RunnableConfig):
    """延迟解析壳：调用时解析 `execute_task` 并原样转发。"""
    return await node_registry.resolve("execute_task")(state, config=config)


async def create_code_query(state: AgentState, *, config: RunnableConfig):
    """延迟解析壳：壳名 `create_code_query`，解析键是 **`code_agent`**（见上方映射注释）。"""
    return await node_registry.resolve("code_agent")(state, config=config)


async def create_research_plan(state: AgentState, *, config: RunnableConfig):
    """延迟解析壳：调用时解析 `create_research_plan` 并原样转发。"""
    return await node_registry.resolve("create_research_plan")(state, config=config)


# DEAD-CODE(P2-5)｜处置=保留·有意不接线：本函数**有意不接线**（2026-09 审计确认，非"待删除"）。
# build_graph 的 add_node 块只注册 10 个节点，不含它；全仓除本定义与顶部模块
# 说明外 0 处引用，连带函数体内 get_llm_client(..., role="hallucination")
# 亦落在不可达代码中。
# **为什么不删**：这是**文档化的未接线能力**——节点清点表把「A6 幻觉校验」
# 列为"现有但未接入任何图"并附了 grep 证据；
# 它是被刻意保留的后续接线点，不是无人认领的残骸。删掉等于抹掉一处已登记的设计意图。
# **为什么不接线**：接线会给每次图执行新增一次 hallucination 校验 LLM 调用，
# 属运行期行为变更 + 成本变更，需独立评估（当前环境无 LLM 服务，无法评估）。
# 取舍：保持"标注 + 运行期断言未接线"（绊线见 tests/test_p24_deadcode_marking.py）。
async def check_hallucinations(
    state: AgentState, *, config: RunnableConfig
) -> dict[str, Any]:
    """Check if the response is supported by retrieved documents."""
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured for hallucination checks.")

    # P0-2(b)：显式绑定 hallucination 角色 → 确定性 profile（temperature=0.0,
    # timeout=15s, max_retries=1）。调用点传 temperature=0.7 → 落到 profile 温度 0.0。
    model = capabilities.resolve("get_llm_client")(
        temperature=0.7, tags=["hallucinations"], role="hallucination"
    )

    system_prompt = CHECK_HALLUCINATIONS.format(
        documents=state.documents,
        generation=state.messages[-1],
    )

    messages = [{"role": "system", "content": system_prompt}] + state.messages

    logger.info("---CHECK HALLUCINATIONS---")

    response = cast(
        GradeHallucinations,
        await model.with_structured_output(GradeHallucinations).ainvoke(messages),
    )

    return {"hallucination": response}


# ---------------------------------------------------------------------------
# Backward-compatible graph compilation
# ---------------------------------------------------------------------------
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph
from agentix.orchestration.langgraph.lg_states import InputState


def create_message_window_node(max_messages: int, token_budget: int = 0):
    """批次 7：主图历史窗口化节点工厂。

    背景：``AgentState.messages`` 走 ``add_messages`` reducer，会话经
    checkpointer（thread_id=session_id）跨请求持久化，旧消息**无界累积**，
    长会话会持续推高每轮的上下文体量与延迟。本节点在每轮入口处把
    ``messages`` 裁剪到最近 ``max_messages`` 条（含本轮新输入的 human 消息），
    裁剪通过 ``RemoveMessage``（add_messages 官方删除语义）完成，checkpointer
    会持久化裁剪后的状态。

    安全性：
    - ``max_messages <= 0`` 时为**纯 no-op**（窗口化关闭，行为与旧版一致）；
    - 未超限时同样 no-op；
    - 无 id 的消息跳过删除（add_messages 会为其补 id，正常不出现）；
    - 裁剪只影响「送入模型的历史窗口」，Redis/DB 里的完整会话记录不受影响。

    上下文工程 C（``token_budget = LG_TOKEN_HISTORY_BUDGET``，默认 0 = 关）：
    条数窗口之上再加一道 **token 级预算**——从最新消息**向前一次性累加**
    ``count_tokens``，累到预算即停（``while`` 反向遍历，每条只算一次，
    避免逐条重算的 O(n²)）；**至少保留最新一条消息**（本轮输入不可裁）。
    ``token_budget <= 0`` ⇒ 该分支完全不执行，与旧实现逐位等价。
    """
    from langchain_core.messages import RemoveMessage

    def _text(message) -> str:
        content = getattr(message, "content", "")
        return content if isinstance(content, str) else str(content)

    async def window_messages(state: AgentState) -> Dict[str, Any]:
        if max_messages <= 0 and token_budget <= 0:
            return {"messages": []}
        msgs = state.messages or []
        if not msgs:
            return {"messages": []}
        # ① 条数窗口（旧逻辑）：确定候选窗口起点
        start = 0
        if max_messages > 0 and len(msgs) > max_messages:
            start = len(msgs) - max_messages
        # ② token 预算窗口（C 阶段新增）：从尾部向前累加，超预算即停。
        #    最新一条无论如何保留（至少 i = len(msgs)-1）。
        if token_budget > 0:
            from agentix.agent.context.token_count import count_tokens

            used = count_tokens(_text(msgs[-1]))
            i = len(msgs) - 2
            while i >= start:
                cost = count_tokens(_text(msgs[i]))
                if used + cost > token_budget:
                    break
                used += cost
                i -= 1
            start = max(start, i + 1)
        if start <= 0:
            return {"messages": []}
        victims = [RemoveMessage(id=m.id) for m in msgs[:start] if m.id]
        if not victims:
            return {"messages": []}
        return {"messages": victims}

    return window_messages


def _wrap_execute_task_in_loop(node_fn):
    """F1e·A-P1-4（统一执行链 Stage F）：execute_task 的进环 HITL 注入壳。

    langgraph 依赖**只在本模块**（p66 LangGraph 白名单）：运行态满足
    ① py3.12+（3.10 async ``interrupt`` 必死锁，见
    ``tests/test_p32_tool_guards.py`` 特征化，故 3.10 诚实降级=不注入、
    沿用 review_required 快速失败）、② ``HITL_IN_LOOP_ENABLED``、
    ③ ``HUMAN_REVIEW_ENABLED``、④ 调用方声明进环意图
    （``configurable.hitl_in_loop``，非 stream ask 注入）四条时，把
    ``langgraph.types.interrupt`` 经 config 递给 ``execute_task`` →
    ``task_execution``：遇 review_required 挂起（checkpointer 快照
    thread_id），恢复值 ``approve`` ⇒ 落批准后重试一次，其余 fail-closed。

    T23（流内审批）：config 里带 ``stream_hitl`` 通道时**优先**用它的
    ``request`` 回调替代 ``interrupt``——流只有一条 SSE 响应，图一挂起就没帧可发，
    故流侧改为节点内等待（等待期由流侧循环下发审批帧/心跳帧）；该回调是 async，
    且不依赖 langgraph ``interrupt`` ⇒ **不受 ①的版本门控**。

    flag-off/不满足时透传原 config——与基线逐位等价。
    """
    import sys as _sys

    async def _in_loop_execute_task(
        state: AgentState, *, config: RunnableConfig
    ) -> Dict[str, Any]:
        cfg_map = config if isinstance(config, dict) else dict(config or {})
        configurable = (
            cfg_map.get("configurable")
            if isinstance(cfg_map.get("configurable"), dict)
            else None
        )
        if configurable is not None and configurable.get("hitl_in_loop"):
            callback = None
            stream_channel = configurable.get("stream_hitl")
            if stream_channel is not None:
                # T23：**流**内审批通道优先。流只有一条 SSE 响应可写——图一挂起
                # （interrupt）就没有后续帧，客户端只能等 idle 超时，所以流侧用
                # 「节点内等待」：回调是 async 的，等待期把审批帧/心跳帧交给流侧
                # 循环下发。它不依赖 langgraph 的 async interrupt ⇒ 不受下面
                # py3.12 版本门控（门控只为 interrupt 的死锁问题存在）。
                callback = stream_channel.request
            elif (
                _sys.version_info >= (3, 12)
                and settings.HITL_IN_LOOP_ENABLED
                and settings.HUMAN_REVIEW_ENABLED
            ):
                from langgraph.types import interrupt

                callback = interrupt
            if callback is not None:
                cfg = dict(cfg_map)
                new_configurable = dict(configurable)
                # 通道本体不往节点里带（节点只需要回调）：config 会被框架与
                # 追踪层一路传下去，少挂一个本层内部对象少一处意外。
                new_configurable.pop("stream_hitl", None)
                new_configurable["hitl_in_loop_interrupt"] = callback
                cfg["configurable"] = new_configurable
                return await node_fn(state, config=cfg)
        return await node_fn(state, config=config)

    return _in_loop_execute_task


async def resume_in_loop(thread_id: str, resume_value: Any) -> Any:
    """F1e·A-P1-4：恢复图内挂起的进环 HITL 线程（/ops 恢复面唯一调用方）。

    langgraph ``Command`` 依赖留本模块（p66 白名单）；``graph`` 迟绑定——
    与 ops_router ``/threads/resume`` 同款语义，启动期 Redis checkpointer
    覆盖后自动跟随最新图。``resume_value`` 原样交给 ``interrupt`` 返回处：
    ``approve`` 落批准后重试，其余值 fail-closed 快速失败。线程无挂起
    （未知 thread / 未开进环）时由图侧抛错，调用方转 409。
    """
    from langgraph.types import Command

    # LG-2-3：config 形状（含顶层 recursion_limit）只从 graph_config 出——恢复路径
    # 此前缺这个键，会落到框架默认 25，与主链路的 MAX_ITERATIONS 各说一套。
    config = thread_config(thread_id)
    # 进环恢复点落 span。thread_id 判据只从 config 取（唯一真源，
    # 约定 6 ④）；fail-open 由 helper 保证（无 span/异常 ⇒ no-op，约定 2）。
    record_resume_from_checkpoint(thread_id_present=bool(config["configurable"].get("thread_id")))
    # LG-3-3：第四个恢复入口（进环 HITL 裁决）。计数点是「向图发起续跑」。
    record_graph_resume("hitl_in_loop")
    return await graph.ainvoke(
        Command(resume=resume_value),
        config=config,
        **durability_kwargs(),
    )


def _node_timeout_kwargs() -> Dict[str, Any]:
    """LG-3-2：主图节点的原生超时实参；**未启用时返回空 dict**。

    返回空 dict 而不是 ``{"timeout": None}`` 是本项的口径：开关为 0 时
    ``add_node`` 完全不传该形参，注册出来的节点与基线逐位等价（约定 1）。
    策略对象只从 ``policies.node_timeout`` 出（LG-0-3 集中点，且
    ``tests/test_p147_policy_centralization.py`` 按实参来源判违规）。

    只对**异步节点**合法：超时依赖 asyncio 协作式取消，而同步节点无法在
    进程内被安全取消——框架在 ``compile().validate()`` 阶段就抛 ``ValueError``，
    不会留到运行期才发现（边界说明见指导文档 §4.4 LG-3-2 验收第 ③ 条）。
    """
    policy = node_timeout(settings.NODE_TIMEOUT_SECONDS)
    return {"timeout": policy} if policy is not None else {}


def _node_error_handler_kwargs() -> Dict[str, Any]:
    """LG-3-5：有副作用节点的原生错误补偿实参；**未启用时返回空 dict**。

    返回空 dict 而不是 ``{"error_handler": None}`` 是本项的口径：开关关闭时
    ``add_node`` 完全不传该形参，注册出来的图与基线逐位等价（约定 1）。
    handler 本体由 ``policies.node_error_handler`` 构造（LG-0-3 集中点）。

    只挂 ``execute_task`` / ``code_agent``：它们是**有副作用**的节点，失败时
    整轮 500 等于把「哪些副作用已经发生」丢给调用方猜。补偿 handler 只写记录
    并收尾，**不重跑节点**，所以与 LG-1-1「这两个节点不挂 retry_policy」不冲突。
    """
    if not settings.NODE_ERROR_HANDLER_ENABLED:
        return {}
    # 补偿记录落库与 T18 共用同一装配判据（hitl_bridge.resolve_pending_store），
    # 不在此另判 flag；import 留在函数内，flag-off 时连 sqlalchemy 都不拉。
    from agentix.policies.hitl.hitl_bridge import resolve_pending_store

    return {"error_handler": node_error_handler(store=resolve_pending_store())}


def build_graph(checkpointer=None):
    """构建并编译主图；checkpointer 由启动期注入（Redis 实例或 None/MemorySaver）。

    P0-1：原模块级编译改为工厂函数，FastAPI 启动期会用 Redis checkpointer
    重编译并覆盖下方模块级 `graph`；checkpointer 为 None 时图不持久化。
    """
    builder = StateGraph(AgentState, input_schema=InputState)
    # LG-1-1：只读节点的原生重试。flag-off ⇒ 下面是**空 dict**，add_node 完全不传
    # `retry_policy` ⇒ 与基线逐位等价（约定 1）；策略实参来自 policies.py（LG-0-3 集中点），
    # 不在本文件内联构造。覆盖面只有 6 个只读节点，`execute_task` / `code_agent` 永不挂
    # （整节点重跑会重放工具副作用）。
    readonly_retry_kwargs = (
        {"retry_policy": readonly_retry_policy()} if settings.NODE_RETRY_ENABLED else {}
    )
    # LG-3-2：节点级原生超时（langgraph 1.x 的 `add_node(timeout=)`）。flag-off
    # （`NODE_TIMEOUT_SECONDS=0`，默认）⇒ 下面是**空 dict** ⇒ add_node 完全不传
    # `timeout` ⇒ 与基线逐位等价（约定 1）。超时是**每次节点尝试**的墙钟上限，
    # 靠 asyncio 协作式取消生效（约定 6 ②：不让出事件循环的节点杀不掉）；
    # `NodeTimeoutError` 已在领域分类里判为不可重试，不会与上面的 retry_policy
    # 叠成「超时→重试→再超时」（LG-3-2 验收第 ④ 条）。
    node_timeout_kwargs = _node_timeout_kwargs()
    # LG-3-5：有副作用节点的原生错误补偿（langgraph 1.x 的 `add_node(error_handler=)`）。
    # flag-off（默认）⇒ 空 dict ⇒ 完全不传该形参（约定 1）；开时仅在
    # execute_task / code_agent 上挂 handler（见 _node_error_handler_kwargs 的边界说明）。
    node_error_handler_kwargs = _node_error_handler_kwargs()
    # 策略形参口径（LG-0-3）：下面这些 add_node 若要挂 retry_policy / cache_policy /
    # error_handler / timeout，实参**只能**来自 agentix/orchestration/langgraph/policies.py
    # 的工厂；在本文件内联 `RetryPolicy(...)` / `TimeoutPolicy(...)` 由
    # tests/test_p147_policy_centralization.py 判违规（散落的字面量改一处漏一处且不报错）。
    # 接线进度：LG-1-1 已给 6 个只读节点挂 retry_policy（见上方 readonly_retry_kwargs）；
    # LG-3-2 已给全部 10 个节点挂 timeout（见 node_timeout_kwargs）；
    # LG-3-5 已给 2 个有副作用节点挂 error_handler（见 node_error_handler_kwargs）；
    # cache_policy 仍未接线（不在本轮范围）。三个 kwargs 都用 `**` 展开，
    # 关时完全不传形参——守卫（p147 判据 2）对 `**` 展开显式豁免的正是这一形态。
    # 注意：check_hallucinations 故意不在此注册（本块仅注册下列 10 个节点：
    # message_window / analyze_and_route_query / respond_to_general_query /
    # get_additional_info / create_research_plan / create_image_query /
    # create_file_query / create_kb_query / execute_task / code_agent），
    # 其定义见本模块上方 check_hallucinations(...)，标注为 DEAD-CODE(P2-5)｜保留·有意不接线。
    # 批次 7：入口插入历史窗口化节点（settings.MAX_HISTORY_MESSAGES<=0 时
    # 为 no-op，行为与旧版一致），裁剪 checkpointer 持久化的无界历史。
    # 上下文工程 C：LG_TOKEN_HISTORY_BUDGET>0 时在条数窗口内再加 token 预算
    # （默认 0 ⇒ 该分支不执行，与基线逐位等价）。
    builder.add_node(
        "message_window",
        create_message_window_node(
            settings.MAX_HISTORY_MESSAGES,
            getattr(settings, "LG_TOKEN_HISTORY_BUDGET", 0),
        ),
        **node_timeout_kwargs,
    )
    builder.add_node(analyze_and_route_query, **readonly_retry_kwargs, **node_timeout_kwargs)
    builder.add_node(respond_to_general_query, **readonly_retry_kwargs, **node_timeout_kwargs)
    builder.add_node(get_additional_info, **node_timeout_kwargs)
    builder.add_node(
        "create_research_plan",
        create_research_plan,
        **readonly_retry_kwargs,
        **node_timeout_kwargs,
    )
    builder.add_node(create_image_query, **readonly_retry_kwargs, **node_timeout_kwargs)
    builder.add_node(create_file_query, **readonly_retry_kwargs, **node_timeout_kwargs)
    builder.add_node(create_kb_query, **readonly_retry_kwargs, **node_timeout_kwargs)
    # P1（任务执行）：execute_task 无条件注册（route_query 的 Literal/ROUTE_HANDLERS
    # 需与节点集对齐，phase19 绊线断言两者相等）；可达性由 TASK_EXEC_ENABLED 在
    # 路由侧门控——flag-off 时任何路由决策都不会进入本节点，行为与基线等价。
    # F1e·A-P1-4：注册时包一层进环注入壳（节点名不变 = 绊线断言不受影响；
    # flag-off/3.10/未声明进环意图时纯透传）。
    builder.add_node(
        "execute_task",
        _wrap_execute_task_in_loop(execute_task),
        **node_error_handler_kwargs,
        **node_timeout_kwargs,
    )
    # P2（代码执行）：code_agent 无条件注册（route_query 的 Literal / ROUTE_HANDLERS
    # 需与节点集对齐，phase19 绊线断言两者相等）；可达性由 CODE_EXEC_ENABLED 在
    # 路由侧门控——flag-off 时任何路由决策都不会进入本节点，行为与基线等价。
    builder.add_node(
        "code_agent",
        create_code_query,
        **node_error_handler_kwargs,
        **node_timeout_kwargs,
    )

    builder.add_edge(START, "message_window")
    builder.add_edge("message_window", "analyze_and_route_query")
    builder.add_conditional_edges("analyze_and_route_query", route_query)
    return builder.compile(checkpointer=checkpointer)


# 向后兼容：模块级默认图（MemorySaver）。
# 独立 CLI 入口（application/agents/main.py）与单元测试依赖此符号；
# FastAPI 启动期若 Redis checkpointer 就绪，会覆盖 lg_builder.graph 为
# Redis 持久化版本——外部访问请用 `lg_builder.graph` 属性（动态取值），
# 不要 `from lg_builder import graph`（值绑定不会跟随启动期覆盖）。
checkpointer = MemorySaver()
graph = build_graph(checkpointer=checkpointer)

# ---------------------------------------------------------------------------
# Phase 19：multi_agent 接线（supervisor 拓扑切换）
# ---------------------------------------------------------------------------
#: supervisor 拓扑编译结果。惰性：仅 flag 开启（或启动期显式重建）时编译；
#: main.py 启动期在 Redis checkpointer 就绪后会同时重建 graph 与本图，
#: 保证两拓扑共享同一 checkpointer（thread_id=session_id 语义一致）。
supervisor_graph: Any = None


def build_supervisor_graph(checkpointer=None):
    """构建 supervisor 多智能体拓扑（Phase 19）。

    实现在 ``agentix.orchestration.langgraph.lg_supervisor``（原 application
    路径已随 M6 删除）；此处惰性导入，
    避免 lg_builder ↔ lg_supervisor 的模块级循环（历史上 lg_supervisor 顶层
    import 本模块；2026-10-29 M3-5b 起该 import 已下移到其调用体内，环两侧各解除）。
    """
    from agentix.orchestration.langgraph.lg_supervisor import (
        build_supervisor_graph as _build_supervisor_graph,
    )

    return _build_supervisor_graph(checkpointer=checkpointer)


def get_active_graph():
    """Phase 19 图选择：``AGENTIX_ABLATION_MULTI_AGENT=true`` ⇒ supervisor 拓扑。

    默认（未注入 / False）⇒ 扁平图 ``graph``——现网零变化（flag-off 等价）。
    经 ``chat_service._graph_getter`` 惰性调用：不缓存开关值，与
    ``ablation.is_enabled`` 每请求读取语义一致（消融子进程注入即生效）。
    """
    global supervisor_graph
    if ablation.is_enabled("multi_agent"):
        if supervisor_graph is None:
            supervisor_graph = build_supervisor_graph(checkpointer=checkpointer)
        return supervisor_graph
    return graph


async def safety_guardrails(state: AgentState, *, config: RunnableConfig) -> Dict[str, List[BaseMessage]]:
    """Backward-compatible wrapper for guardrails logic."""
    return await get_additional_info(state, config=config)
