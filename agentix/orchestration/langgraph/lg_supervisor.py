"""Supervisor 多智能体拓扑（Phase 19）—— ``multi_agent`` 消融开关的可切换目标。

与扁平图（``lg_builder.build_graph``）的差异
--------------------------------------------
* 扁平：``message_window → analyze_and_route_query → 条件边 → 单个 handler``；
* supervisor：``message_window → analyze_and_route_query → supervisor_dispatch``，
  节点内部用既有 ``Manager/Peer``（``agentix.orchestration.strategies.multi_agent``，Phase 11 测通）
  协调 **Proposer**（= 被路由到的 handler，即子 agent，一次执行产出草稿）与
  **Reviewer**（结构化审阅；``OPENAI_API_KEY`` 可用时升级为 LLM 审阅 +
  修订轮）。六失败模式与信息增量随结果携带（见 ``additional_kwargs["multi_agent"]``）。

诚实性约定（Strangler）
-----------------------
* ``multi_agent`` 默认 ``False``（``ablation.FLAG_DEFAULTS``）—— flag-off
  走扁平图，现网零变化；图选择在 ``lg_builder.get_active_graph``（惰性
  ``graph_getter`` 每请求读取，与 ``ablation.is_enabled`` 语义一致）。
* 离线（无 ``OPENAI_API_KEY``）走**确定性结构审阅**，不发起 LLM 调用；
  Reviewer 否决而 Manager 未收敛时 **fail-open 回退子 agent 草稿**
  （与扁平图同质量，绝不更差），否决事实记入 meta 供观测。
* Reviewer 是「答案质量」审阅，不是安全闸门——安全闸门在 Phase 16 HITL。
* 子代理隔离（Stage F2c·A-P2-1，``MULTIAGENT_SUBAGENT_ISOLATION_ENABLED``
  默认关）：flag-on ⇒ handler 子 agent 收任务面 state（父对话轨迹截除）、
  config 摘除活 ``memory_context``（skip_memory，唯一记忆入口 =
  ``MemorySnapshot`` 只读快照）、Proposer/Reviewer 各持独立
  ``SubAgentSession``（session_id + 独立 budget）经心跳保活与 live registry
  kill/pause/steer 控制；flag-off 全部旁路，与基线逐位等价。
"""

from __future__ import annotations

from typing import Any, Dict

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import START, StateGraph
from loguru import logger

from agentix.orchestration.strategies.multi_agent import (
    AgentRole,
    InProcessBus,
    Manager,
    ManagerConfig,
    PeerBudget,
    PeerResult,
    information_increment,
)
from agentix.orchestration.strategies.multi_agent.isolation import (
    IsolationPolicy,
    LiveSubagentRegistry,
    SubAgentSession,
    ephemeral_task_face_state,
    isolated_config,
    session_budgets,
    take_memory_snapshot,
    wrap_peer,
)
from agentix.orchestration.langgraph import lg_builder
from agentix.application.agents.guardrails import get_additional_info
from agentix.application.agents.file_handler import create_file_query
from agentix.application.agents.general_handler import (
    create_kb_query,
    respond_to_general_query,
)
from agentix.application.agents.image_handler import create_image_query
from agentix.orchestration.langgraph.lg_states import AgentState, InputState
from agentix.application.agents.router import analyze_and_route_query, route_query
from agentix.application.agents.task_handler import execute_task
from agentix.config import settings

__all__ = [
    "build_supervisor_graph",
    "supervisor_dispatch",
    "ROUTE_HANDLERS",
]

#: ``route_query`` 的 7 个目标节点 → handler。与扁平图条件边同一收口，
#: 保证 supervisor 与扁平的「路由 → 子 agent」映射逐位一致。
#: 测试可直接替换条目（勿 monkeypatch lg_builder 属性——本 dict 已持有引用）。
ROUTE_HANDLERS: Dict[str, Any] = {
    "respond_to_general_query": respond_to_general_query,
    "get_additional_info": get_additional_info,
    "create_research_plan": lg_builder.create_research_plan,
    "create_image_query": create_image_query,
    "create_file_query": create_file_query,
    "create_kb_query": create_kb_query,
    "execute_task": execute_task,
}

#: supervisor 内 Manager 的轮次上限：结构路径第 1 轮必收敛；
#: LLM 审阅否决 → 第 2 轮修订 → 第 3 轮复审。3 轮封顶防成本失控。
_SUPERVISOR_MAX_ROUNDS = 3

_REVIEW_SYSTEM = (
    "你是测试数据问答的审阅者。只依据给出的目标与候选答案做结构化审阅：\n"
    "1. 首行以 approve 或 reject 开头；reject 必须附简短理由；\n"
    "2. 判定标准：答案为空、与目标无关、明显编造数据条目/引用 → reject；\n"
    "3. 不要重写答案，不要输出任何其他内容。"
)

_REVISE_SYSTEM = (
    "你是测试数据问答的修订者。根据审阅意见修订候选答案，只输出修订后的答案正文：\n"
    "1. 保留候选答案中正确的部分；针对审阅意见修正问题；\n"
    "2. 不得编造数据条目与引用；不得输出与答案无关的说明。"
)


def _llm_available() -> bool:
    """LLM 审阅/修订的开关：无 key ⇒ 纯结构审阅（离线默认）。"""
    return bool(getattr(settings, "OPENAI_API_KEY", ""))


def _budget_factory(agent_id: str) -> PeerBudget:
    """Phase 25：Peer 独立预算单源（settings 注入，默认 = PeerBudget 现值）。

    application 组装点读 settings（agent/ 不反向依赖 application）；
    ``MULTIAGENT_PEER_MAX_STEPS/MESSAGES`` 默认 10/20 与 ``PeerBudget()``
    默认逐位一致 ⇒ 未配置时行为与接线前等价（有测试断言防漂移）。
    """
    return PeerBudget(
        max_steps=int(getattr(settings, "MULTIAGENT_PEER_MAX_STEPS", 10) or 10),
        max_messages=int(getattr(settings, "MULTIAGENT_PEER_MAX_MESSAGES", 20) or 20),
    )


def _make_bus() -> Any:
    """Phase 25：进程内消息总线（``MULTIAGENT_BUS_ENABLED`` 默认关 = None）。"""
    if not getattr(settings, "MULTIAGENT_BUS_ENABLED", False):
        return None
    return InProcessBus()


def _isolation_enabled() -> bool:
    """Stage F2c·A-P2-1：子代理隔离三件套总闸（默认关 = 现行为逐位等价）。"""
    return bool(getattr(settings, "MULTIAGENT_SUBAGENT_ISOLATION_ENABLED", False))


def _new_isolation() -> tuple:
    """flag-on 组装点：live registry + proposer/reviewer 两个独立会话。

    每会话独立 ``session_id`` + 独立 ``PeerBudget``（经 settings 单源镜像），
    预算随会话生命周期归属（注入 Manager ``budgets``，见 ``session_budgets``）。
    """
    policy = IsolationPolicy()
    registry = LiveSubagentRegistry(policy=policy)
    sessions = tuple(
        registry.register(
            SubAgentSession(actor=actor, budget=_budget_factory(actor), policy=policy)
        )
        for actor in ("proposer", "reviewer")
    )
    return registry, policy, sessions


def _parse_verdict(text: str) -> str:
    """把 LLM 审阅输出解析为 approve/reject（与 Manager._extract_decision 同口径）。

    不可解析 ⇒ ``reject``（审阅层保守），但上层 fail-open 回退草稿，
    用户可见结果不因此变差。
    """
    t = (text or "").strip().lower()
    for token in ("reject", "rejected"):
        if t.startswith(token) or f" {token}" in t[:40]:
            return "reject"
    for token in ("approve", "accept", "ok"):
        if t.startswith(token) or f" {token}" in t[:40]:
            return "approve"
    return "reject"


def _structural_verdict(proposal: str) -> str:
    """确定性结构审阅（离线路径）：空答案不可用，其余放行。"""
    if not (proposal or "").strip():
        return "reject: 空答案"
    return "approve"


async def _llm_review(*, goal: str, proposal: str) -> str:
    from agentix.application.agents.llm_factory import get_llm_client

    llm = get_llm_client(temperature=0.0, tags=["multi_agent_review"], role="router")
    messages = [
        {"role": "system", "content": _REVIEW_SYSTEM},
        {"role": "user", "content": f"目标：{goal}\n\n候选答案：{proposal}"},
    ]
    response = await llm.ainvoke(messages)
    return str(response.content)


async def _llm_revise(*, goal: str, proposal: str, feedback: str) -> str:
    from agentix.application.agents.llm_factory import get_llm_client

    llm = get_llm_client(temperature=0.7, tags=["multi_agent_revise"], role="general")
    messages = [
        {"role": "system", "content": _REVISE_SYSTEM},
        {
            "role": "user",
            "content": f"目标：{goal}\n\n候选答案：{proposal}\n\n审阅意见（黑板上下文）：{feedback}",
        },
    ]
    response = await llm.ainvoke(messages)
    return str(response.content or "").strip()


def _incremental_ratio(messages: Any) -> float:
    """黑板消息的信息增量占比（Phase 19 指标：离线聚合用）。"""
    msgs = list(messages or [])
    if not msgs:
        return 0.0
    ctx = ""
    incremental = 0
    for m in msgs:
        inc = information_increment(str(getattr(m, "content", "") or ""), ctx)
        if inc["incremental"]:
            incremental += 1
        ctx = f"{ctx}\n{m.content}" if ctx else str(getattr(m, "content", "") or "")
    return round(incremental / len(msgs), 4)


async def supervisor_dispatch(
    state: AgentState, *, config: RunnableConfig
) -> Dict[str, Any]:
    """supervisor 节点：路由 → 子 agent 执行 → Manager(Proposer/Reviewer) 协调。

    返回与扁平 handler 同形的状态增量（``messages`` + handler 自带键），
    末条消息 ``additional_kwargs["multi_agent"]`` 携带：
    ``handler/ok/rounds/failure_mode/reason/incremental_ratio/status/exit_reason/truncated``；
    flag ``MULTIAGENT_SUBAGENT_ISOLATION_ENABLED`` 开启时另携带 ``isolation``
    （策略 / 会话 / 只读快照规模 / live registry 观测，见 §A-P2-1）。
    """
    goal = state.messages[-1].content if state.messages else ""
    handler_name = route_query(state)
    handler = ROUTE_HANDLERS.get(handler_name)
    if handler is None:  # route_query 的 Literal 收口，理论不可达
        raise ValueError(f"supervisor_dispatch: unknown route {handler_name!r}")

    # Stage F2c·A-P2-1（flag 默认关 = 下两行逐位等价原路径）：
    # flag-on ⇒ 子 agent 只见任务面 state（父对话轨迹不进）+ config 摘除
    # 活 memory_context（skip_memory，只读快照改由 memory_snapshot 键提供）。
    isolation_on = _isolation_enabled()
    registry: Any = None
    iso_policy: Any = None
    iso_sessions: tuple = ()
    mem_snapshot: Any = None
    if isolation_on:
        registry, iso_policy, iso_sessions = _new_isolation()
        configurable = (
            (config or {}).get("configurable") if isinstance(config, dict) else None
        )
        mem_snapshot = take_memory_snapshot((configurable or {}).get("memory_context"))
        exec_state = ephemeral_task_face_state(state)
        exec_config = isolated_config(config, mem_snapshot)
    else:
        exec_state, exec_config = state, config

    # 子 agent 执行（与扁平图条件边等价的**一次** handler 调用）
    handler_result: Dict[str, Any] = dict(
        await handler(exec_state, config=exec_config)
    )
    msgs = list(handler_result.get("messages") or [])
    draft = str(msgs[-1].content) if msgs else ""

    async def proposer(*, goal: str, context: str, round: int, **kw: Any) -> Any:
        if round <= 1:
            return PeerResult(
                agent_id="proposer",
                role=AgentRole.PROPOSER,
                output=draft,
                meta={"decision": "accept"},
            )
        # 修订轮：仅 LLM 可用且审阅否决后才会到达（离线直接回草稿）
        revised = ""
        if _llm_available():
            try:
                revised = await _llm_revise(goal=goal, proposal=draft, feedback=context)
            except Exception as exc:  # noqa: BLE001 —— 修订失败退回草稿，不挂链路
                logger.warning("supervisor: revise failed, keep draft: {}", exc)
                revised = ""
        return PeerResult(
            agent_id="proposer",
            role=AgentRole.PROPOSER,
            output=revised or draft,
            meta={"decision": "accept", "revised": bool(revised)},
        )

    async def reviewer(*, goal: str, proposal: str, context: str, round: int, **kw: Any) -> Any:
        verdict = _structural_verdict(proposal)
        if verdict != "approve":
            return PeerResult(
                agent_id="reviewer",
                role=AgentRole.REVIEWER,
                output=verdict,
                meta={"decision": "reject"},
            )
        if not _llm_available():
            return PeerResult(
                agent_id="reviewer",
                role=AgentRole.REVIEWER,
                output="approve: 结构审阅通过（离线）",
                meta={"decision": "approve"},
            )
        try:
            review_text = await _llm_review(goal=goal, proposal=proposal)
        except Exception as exc:  # noqa: BLE001 —— 审阅失败 fail-open 到草稿质量线
            logger.warning("supervisor: LLM review failed, fail-open approve: {}", exc)
            review_text = "approve"
        return PeerResult(
            agent_id="reviewer",
            role=AgentRole.REVIEWER,
            output=review_text or "approve",
            meta={"decision": _parse_verdict(review_text)},
        )

    # stop_on_contradiction=False：Proposer=accept 而 Reviewer=reject 是
    # **修订轮的触发器**（默认 True 会首轮即停、永不修订）；未收敛的矛盾
    # 仍会在轮次耗尽时被 detect_failure_mode 记为 CONTRADICTION（六模式不丢）。
    manager_kwargs: Dict[str, Any] = {}
    if isolation_on:
        # flag-on：Peer 包隔离闸（心跳 / kill 短路 / pause 有界等待 /
        # steer 注入 / 只读记忆快照段）+ 会话独立预算单源。
        proposer = wrap_peer(
            proposer,
            registry=registry,
            session=iso_sessions[0],
            role=AgentRole.PROPOSER,
            snapshot=mem_snapshot,
        )
        reviewer = wrap_peer(
            reviewer,
            registry=registry,
            session=iso_sessions[1],
            role=AgentRole.REVIEWER,
            snapshot=mem_snapshot,
        )
        manager_kwargs["budgets"] = session_budgets(iso_sessions)
    manager = Manager(
        config=ManagerConfig(
            max_rounds=_SUPERVISOR_MAX_ROUNDS, stop_on_contradiction=False
        ),
        proposer=proposer,
        reviewer=reviewer,
        budget_factory=_budget_factory,
        bus=_make_bus(),
        **manager_kwargs,
    )
    outcome = None
    try:
        outcome = await manager.run(goal)
    except Exception as exc:  # noqa: BLE001 —— Manager 异常同样回退草稿
        logger.warning("supervisor: manager.run failed, fallback to draft: {}", exc)

    # Phase 25：冲突/级联**标记**透传（只报告不合并不转述，见 ManagerOutcome.meta）
    outcome_meta: Dict[str, Any] = (outcome.meta or {}) if outcome is not None else {}
    defense_meta: Dict[str, Any] = {
        "conflict_marked": bool(outcome_meta.get("conflict_marked", False)),
        "conflicts": list(outcome_meta.get("conflicts", [])),
    }
    if outcome_meta.get("bus") is not None:
        defense_meta["bus"] = outcome_meta["bus"]
    # Phase H · W5：子代理结果契约（SUBAGENT_RESULT_CONTRACT_ENABLED flag-on
    # 才非空；空不挂键 = flag-off 时 meta 与基线逐位等价）。
    if outcome is not None and getattr(outcome, "result_contracts", None):
        defense_meta["result_contracts"] = list(outcome.result_contracts)

    if outcome is not None and outcome.ok:
        final_text = outcome.final_output or draft
        meta = {
            "handler": handler_name,
            "ok": True,
            "rounds": outcome.rounds,
            "failure_mode": outcome.failure_mode.value,
            "reason": outcome.reason,
            "incremental_ratio": _incremental_ratio(outcome.messages),
            # Stage F2c·A-P2-2：统一退出契约透传（词表见 domain/runtime/lifecycle）
            "status": outcome.exit_status,
            "exit_reason": outcome.exit_reason,
            "truncated": outcome.truncated,
            **defense_meta,
        }
    else:
        # fail-open：回退子 agent 草稿（= 扁平图交付质量），否决/失败记 meta
        final_text = draft
        meta = {
            "handler": handler_name,
            "ok": False,
            "rounds": outcome.rounds if outcome is not None else 0,
            "failure_mode": (
                outcome.failure_mode.value if outcome is not None else "manager_exception"
            ),
            "reason": outcome.reason if outcome is not None else "manager_run_exception",
            "incremental_ratio": _incremental_ratio(
                outcome.messages if outcome is not None else ()
            ),
            # Manager 未产出 ⇒ failed/error（异常路径不冒充完成态）
            "status": outcome.exit_status if outcome is not None else "failed",
            "exit_reason": outcome.exit_reason if outcome is not None else "error",
            "truncated": bool(outcome.truncated) if outcome is not None else False,
            **defense_meta,
        }
        logger.info(
            "supervisor: fallback to draft handler={} failure_mode={} reason={}",
            handler_name,
            meta["failure_mode"],
            meta["reason"],
        )

    # Stage F2c·A-P2-1（仅 flag-on 追加：策略 / 会话 / 快照规模 / registry 观测）
    if isolation_on:
        meta["isolation"] = {
            "policy": iso_policy.to_dict(),
            "sessions": [session.to_dict() for session in iso_sessions],
            "snapshot_chars": (
                mem_snapshot.chars if mem_snapshot is not None else 0
            ),
            "registry": registry.status(),
        }

    if msgs:
        last = msgs[-1]
        kwargs = dict(getattr(last, "additional_kwargs", None) or {})
        kwargs["multi_agent"] = meta
        msgs[-1] = AIMessage(
            content=final_text,
            additional_kwargs=kwargs,
            id=getattr(last, "id", None),
        )
    else:
        msgs = [AIMessage(content=final_text, additional_kwargs={"multi_agent": meta})]
    handler_result["messages"] = msgs
    return handler_result


def build_supervisor_graph(checkpointer=None):
    """编译 supervisor 拓扑（与 ``lg_builder.build_graph`` 同 checkpointer 约定）。

    节点：``message_window``（历史窗口化，同扁平）→ ``analyze_and_route_query``
    → ``supervisor_dispatch``（handler 不再是图节点，见 ROUTE_HANDLERS）。
    """
    builder = StateGraph(AgentState, input=InputState)
    builder.add_node(
        "message_window",
        lg_builder.create_message_window_node(
            settings.MAX_HISTORY_MESSAGES,
            getattr(settings, "LG_TOKEN_HISTORY_BUDGET", 0),
        ),
    )
    builder.add_node(analyze_and_route_query)
    builder.add_node("supervisor_dispatch", supervisor_dispatch)
    builder.add_edge(START, "message_window")
    builder.add_edge("message_window", "analyze_and_route_query")
    builder.add_edge("analyze_and_route_query", "supervisor_dispatch")
    return builder.compile(checkpointer=checkpointer)
