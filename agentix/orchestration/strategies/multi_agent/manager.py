"""六失败模式检测与 Manager 协调（Phase 11）。

书 §10.1–10.17 六失败模式（首期覆盖可机检子集）：

1. ``deadlock``        —— 互相等待 / 零进展循环
2. ``livelock``        —— 消息在转但信息增量恒 0
3. ``echo_chamber``    —— Peer 互相复读（高 overlap）
4. ``budget_bleed``    —— 子 Agent 超独立预算拖垮全局
5. ``infinite_escalation`` —— Manager 无深度上限地再委托
6. ``contradiction``   —— Proposer/Reviewer 结论冲突未收敛

Manager：Peer+Manager 拓扑；每 Peer **独立预算/深度/取消**；
信息增量为 0 的消息不转发（防 livelock/echo）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from agentix.domain.runtime.lifecycle import ExitReason, ExitStatus
from agentix.observability.metrics import record_multiagent_failure
from agentix.orchestration.strategies.multi_agent.contract import (
    SubAgentResult,
    contract_enabled,
    contract_face,
    early_exit_enabled,
    evidence_sufficient,
    try_offload_face,
)
from agentix.orchestration.strategies.multi_agent.peer import (
    AgentRole,
    PeerMessage,
    PeerResult,
    information_increment,
)

__all__ = [
    "FailureMode",
    "detect_failure_mode",
    "detect_artifact_conflicts",
    "bound_result_face",
    "peer_face",
    "PeerBudget",
    "ManagerConfig",
    "ManagerOutcome",
    "Manager",
    "derive_exit_contract",
]


class FailureMode(str, Enum):
    NONE = "none"
    DEADLOCK = "deadlock"
    LIVELOCK = "livelock"
    ECHO_CHAMBER = "echo_chamber"
    BUDGET_BLEED = "budget_bleed"
    INFINITE_ESCALATION = "infinite_escalation"
    CONTRADICTION = "contradiction"


@dataclass
class PeerBudget:
    """Peer 独立预算（书：独立预算/深度/取消）。"""

    max_steps: int = 10
    max_messages: int = 20
    max_depth: int = 3  # 允许的再委托深度

    def exhausted_steps(self, used: int) -> bool:
        return self.max_steps > 0 and used >= self.max_steps

    def exhausted_messages(self, sent: int) -> bool:
        return self.max_messages > 0 and sent >= self.max_messages


@dataclass
class ManagerConfig:
    max_rounds: int = 6
    max_escalation_depth: int = 3
    min_information_increment: bool = True
    # 冲突收敛：Reviewer reject 且无修改 → Manager 记 contradiction 并停
    stop_on_contradiction: bool = True
    # L3 Isolation（结果面摘要协议）：Peer 消息进 Manager 的单条上限；
    # <=0 关闭（全量透传）。完整输出仍在 ManagerOutcome.peer_results。
    max_result_chars: int = 4000


@dataclass
class ManagerOutcome:
    ok: bool
    final_output: str
    failure_mode: FailureMode
    rounds: int
    messages: List[PeerMessage] = field(default_factory=list)
    peer_results: List[PeerResult] = field(default_factory=list)
    reason: str = ""
    #: Phase 25：冲突/级联**标记**（只报告不合并不转述——反级联）
    meta: Dict[str, Any] = field(default_factory=dict)
    #: Stage F2c·A-P2-2：统一退出契约（与 LoopResult/TaskOutcome 同词表，
    #: 见 ``domain/runtime/lifecycle.py``）。空值 ⇒ ``__post_init__`` 按
    #: ``ok/reason`` 推导；``truncated`` 恒按契约等式重算，不允许漂移。
    exit_status: str = ""
    exit_reason: str = ""
    truncated: bool = False
    #: 子代理结果契约（flag ``SUBAGENT_RESULT_CONTRACT_ENABLED``
    #: flag-on 才非空；每项 = ``{status, claims, evidence_refs, artifact_refs,
    #: confidence, cost}``，见 ``contract.SubAgentResult``）。缺省空不破
    #: ``__post_init__`` 退出契约。
    result_contracts: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.exit_status or not self.exit_reason:
            status, reason = derive_exit_contract(self.ok, self.reason)
            self.exit_status = self.exit_status or status
            self.exit_reason = self.exit_reason or reason
        # 契约等式（A-P0-3）：truncated == (exit_reason == max_iterations)
        self.truncated = self.exit_reason == ExitReason.MAX_ITERATIONS.value
        if self.failure_mode is not FailureMode.NONE:
            #: A-P2-2：六失败模式检测保留并打点（NONE 不计）
            record_multiagent_failure(self.failure_mode.value)


def derive_exit_contract(ok: bool, reason: str) -> tuple:
    """按 Manager ``reason`` 推导统一退出契约 ``(exit_status, exit_reason)``。

    词表与值域复用 :class:`ExitStatus` / :class:`ExitReason`（单源，不另造）：

    - ``converged`` → completed / completed；
    - 预算耗尽与轮次耗尽 → failed / max_iterations（truncated=True）；
    - peer 粒度取消 → interrupted / interrupted；
    - 未接线 / 无进展 / 矛盾未收敛 / 其它 → failed / error（ok=True 兜底 completed）。
    """
    r = reason or ""
    if r == "converged":
        return ExitStatus.COMPLETED.value, ExitReason.COMPLETED.value
    if r in ("proposer_budget_exhausted", "reviewer_budget_exhausted"):
        return ExitStatus.FAILED.value, ExitReason.MAX_ITERATIONS.value
    if r == "max_rounds_exceeded":
        return ExitStatus.FAILED.value, ExitReason.MAX_ITERATIONS.value
    if r.startswith("peer_cancelled"):
        return ExitStatus.INTERRUPTED.value, ExitReason.INTERRUPTED.value
    if ok:
        return ExitStatus.COMPLETED.value, ExitReason.COMPLETED.value
    return ExitStatus.FAILED.value, ExitReason.ERROR.value


def bound_result_face(text: str, *, max_chars: int = 4000) -> str:
    """L3 Isolation 结果面摘要协议（书：子代理只回摘要）。

    有界 + **显式截断提示**（不静默）；语义级摘要属 L4 Auto-compact（另期，
    默认关）——本函数只做确定性截断，不伪装成"摘要"。

    ``max_chars <= 0`` → 全量透传（关闭）。
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    dropped = len(text) - max_chars
    return text[:max_chars] + f"\n…[结果面已截断，省略 {dropped} 字符]"


def peer_face(value: Any, *, max_chars: int) -> str:
    """Peer 结果进黑板/下一对方的面（单缝）。

    - flag-off（默认）/ 裸字符串结果 → 基线 ``bound_result_face`` 全文截断，逐位等价；
    - flag-on → 结构化契约面（紧凑 JSON，证据经 ``obs://`` 外置）；过
      ``max_chars`` 先整体外置换可取回句柄，外置失败降级基线截断。
    """
    raw = str(value.output if isinstance(value, PeerResult) else value)
    if not contract_enabled() or not isinstance(value, PeerResult):
        return bound_result_face(raw, max_chars=max_chars)
    face = contract_face(value)
    if max_chars > 0 and len(face) > max_chars:
        offloaded = try_offload_face(face)
        if offloaded is not None:
            return offloaded
    return bound_result_face(face, max_chars=max_chars)


def detect_failure_mode(
    *,
    rounds_no_progress: int = 0,
    messages_with_zero_increment: int = 0,
    echo_overlap: float = 0.0,
    peer_budget_exhausted: bool = False,
    escalation_depth: int = 0,
    max_escalation_depth: int = 3,
    contradiction: bool = False,
    max_rounds: int = 6,
    rounds: int = 0,
) -> FailureMode:
    """按优先级返回首要失败模式（可机检子集；全 0 → NONE）。"""
    if contradiction:
        return FailureMode.CONTRADICTION
    if peer_budget_exhausted:
        return FailureMode.BUDGET_BLEED
    if escalation_depth > max_escalation_depth:
        return FailureMode.INFINITE_ESCALATION
    if rounds >= max_rounds and rounds_no_progress > 0:
        return FailureMode.DEADLOCK
    if rounds_no_progress >= 3:
        return FailureMode.DEADLOCK
    if messages_with_zero_increment >= 3 and echo_overlap >= 0.9:
        return FailureMode.ECHO_CHAMBER
    if messages_with_zero_increment >= 3:
        return FailureMode.LIVELOCK
    if echo_overlap >= 0.95:
        return FailureMode.ECHO_CHAMBER
    return FailureMode.NONE


def detect_artifact_conflicts(results: List[PeerResult]) -> List[Dict[str, Any]]:
    """逻辑级冲突启发式（Phase 25，Ch10 §10.17）：**只标记，不合并、不转述**。

    两类离线可测的信号：

    1. **共享产物 key 冲突**：peer 结果 ``meta["artifacts"]`` 中同 key 不同值
       （文件层看不见的语义冲突）；
    2. **结论极性相反**：各 agent **最后一次** decision 出现 accept 与 reject
       并存（未收敛矛盾；修订轮收敛后 reviewer 末次为 approve，不触发）。

    纯函数、零 I/O。冲突进 ``ManagerOutcome.meta``——**标记而非沿链转述**
    （反级联：不把矛盾值往下传，由上层观测裁决）。
    """
    conflicts: List[Dict[str, Any]] = []

    # 1) artifacts：同 key 不同值
    values_by_key: Dict[str, Dict[str, List[str]]] = {}
    for result in results:
        artifacts = (result.meta or {}).get("artifacts")
        if not isinstance(artifacts, dict):
            continue
        for key, value in artifacts.items():
            bucket = values_by_key.setdefault(str(key), {})
            bucket.setdefault(str(value), []).append(result.agent_id)
    for key, bucket in sorted(values_by_key.items()):
        if len(bucket) > 1:
            conflicts.append(
                {
                    "type": "artifact",
                    "key": key,
                    "values": [
                        {"value": val, "agents": sorted(set(agents))}
                        for val, agents in sorted(bucket.items())
                    ],
                }
            )

    # 2) polarity：各 agent 末次 decision 的相反极性
    last_decision: Dict[str, str] = {}
    for result in results:
        decision = str((result.meta or {}).get("decision", "")).lower()
        if decision:
            last_decision[result.agent_id] = decision
    accepted = {
        agent
        for agent, decision in last_decision.items()
        if decision in ("accept", "accept_final", "approve", "ok")
    }
    rejected = {
        agent
        for agent, decision in last_decision.items()
        if decision in ("reject", "rejected")
    }
    if accepted and rejected:
        conflicts.append(
            {
                "type": "polarity",
                "accept": sorted(accepted),
                "reject": sorted(rejected),
            }
        )
    return conflicts


class Manager:
    """Peer+Manager 协调器。

    - 每个 Peer 挂独立 :class:`PeerBudget`；
    - 信息增量为 0 的回复**不**进入共享黑板（防 livelock/echo）；
    - 深度超限 → ``INFINITE_ESCALATION``；
    - Proposer=accept 且 Reviewer=reject → ``CONTRADICTION``（可停）。
    """

    def __init__(
        self,
        *,
        config: Optional[ManagerConfig] = None,
        budgets: Optional[Dict[str, PeerBudget]] = None,
        proposer: Optional[Callable[..., Any]] = None,
        reviewer: Optional[Callable[..., Any]] = None,
        budget_factory: Optional[Callable[[str], PeerBudget]] = None,
        bus: Optional[Any] = None,
    ) -> None:
        self.config = config or ManagerConfig()
        self.budgets = budgets or {}
        self._proposer = proposer
        self._reviewer = reviewer
        #: Phase 25：预算单源工厂（application 组装点注入 settings 值；
        #: None = 现行为 PeerBudget() 默认，逐位等价）
        self._budget_factory = budget_factory
        #: Phase 25：进程内消息总线（None = 点对点直连现行为逐位等价）
        self._bus = bus
        self.blackboard: List[str] = []
        self.messages: List[PeerMessage] = []

    def budget_for(self, agent_id: str) -> PeerBudget:
        if agent_id not in self.budgets:
            if self._budget_factory is not None:
                self.budgets[agent_id] = self._budget_factory(agent_id)
            else:
                self.budgets[agent_id] = PeerBudget()
        return self.budgets[agent_id]

    def _peer_cancelled(self, agent_id: str) -> bool:
        """peer 粒度取消（bus 提供；无 bus ⇒ 永不取消，现行为不变）。"""
        return self._bus is not None and bool(self._bus.is_cancelled(agent_id))

    def _outcome(
        self,
        *,
        ok: bool,
        final_output: str,
        failure_mode: FailureMode,
        rounds: int,
        results: List[PeerResult],
        reason: str,
    ) -> ManagerOutcome:
        """统一构造 outcome：附冲突标记 meta（只标记不转述）+ bus 观测。"""
        conflicts = detect_artifact_conflicts(results)
        meta: Dict[str, Any] = {
            "conflicts": conflicts,
            "conflict_marked": bool(conflicts),
        }
        if self._bus is not None:
            meta["bus"] = self._bus.status()
        contracts: List[Dict[str, Any]] = []
        if contract_enabled():
            # flag-on 时从 PeerResult 派生结果契约（latency/tokens
            # 取 peer 自报 meta，缺省 0——不臆造测量值）。
            contracts = [
                SubAgentResult.from_peer_result(result).to_dict()
                for result in results
            ]
        return ManagerOutcome(
            ok=ok,
            final_output=final_output,
            failure_mode=failure_mode,
            rounds=rounds,
            messages=list(self.messages),
            peer_results=results,
            reason=reason,
            meta=meta,
            result_contracts=contracts,
        )

    def _record(self, msg: PeerMessage) -> bool:
        """记录消息；信息增量不足则丢弃（返回是否入板）。"""
        if self._bus is not None:
            # flag-on：消息经 bus 投递（send 失败只计数不抛——观测不影响主链路）
            self._bus.send(msg)
        context = "\n".join(self.blackboard)
        if self.config.min_information_increment and self.blackboard:
            inc = information_increment(msg.content, context)
            if not inc["incremental"]:
                self.messages.append(msg)  # 仍记审计，但不入板
                return False
        self.messages.append(msg)
        self.blackboard.append(msg.content)
        return True

    async def run(
        self,
        goal: str,
        *,
        proposer: Optional[Callable[..., Any]] = None,
        reviewer: Optional[Callable[..., Any]] = None,
        depth: int = 0,
    ) -> ManagerOutcome:
        prop = proposer or self._proposer
        rev = reviewer or self._reviewer
        if prop is None or rev is None:
            return ManagerOutcome(
                ok=False,
                final_output="",
                failure_mode=FailureMode.NONE,
                rounds=0,
                reason="proposer_or_reviewer_not_wired",
            )

        # 预算耗尽检查（各 Peer 独立）
        p_id, r_id = "proposer", "reviewer"
        if self._bus is not None:
            # 确保双方有信箱（幂等）——否则 _record 的 send 只会计 dropped
            self._bus.subscribe(p_id)
            self._bus.subscribe(r_id)
        pb, rb = self.budget_for(p_id), self.budget_for(r_id)
        rounds = 0
        no_progress = 0
        zero_inc_msgs = 0
        echo = 0.0
        results: List[PeerResult] = []
        contradiction = False

        while rounds < self.config.max_rounds:
            rounds += 1

            # -- Proposer --
            if self._peer_cancelled(p_id):
                return self._outcome(
                    ok=False,
                    final_output=self._joined(),
                    failure_mode=FailureMode.NONE,
                    rounds=rounds,
                    results=results,
                    reason=f"peer_cancelled:{p_id}",
                )
            if pb.exhausted_steps(rounds) or pb.exhausted_messages(rounds):
                mode = detect_failure_mode(
                    peer_budget_exhausted=True,
                    rounds=rounds,
                    max_rounds=self.config.max_rounds,
                    rounds_no_progress=no_progress,
                )
                return self._outcome(
                    ok=False,
                    final_output=self._joined(),
                    failure_mode=mode,
                    rounds=rounds,
                    results=results,
                    reason="proposer_budget_exhausted",
                )
            p_out = await self._call(prop, goal=goal, context=self._joined(), round=rounds)
            board_before = self._joined()
            p_msg = PeerMessage(
                sender=p_id,
                receiver=r_id,
                role=AgentRole.PROPOSER,
                content=peer_face(p_out, max_chars=self.config.max_result_chars),
            )
            entered = self._record(p_msg)
            if not entered:
                zero_inc_msgs += 1
            if isinstance(p_out, PeerResult):
                p_out.messages_sent = p_out.messages_sent + (p_msg,)
                results.append(p_out)
            accept = _extract_decision(p_out, default="accept")

            # 证据充分早停（``MULTIAGENT_EARLY_EXIT_ENABLED``
            # 默认关 = 现行为逐位等价）。顺序拓扑下「停其余 peer」= 跳过
            # 本 peer（reviewer）与后续轮次；bus.cancel 不适用（无在途消息）。
            if early_exit_enabled() and evidence_sufficient(p_out, board_before):
                return self._outcome(
                    ok=True,
                    final_output=p_msg.content,
                    failure_mode=FailureMode.NONE,
                    rounds=rounds,
                    results=results,
                    reason="evidence_sufficient",
                )

            # -- Reviewer --
            if self._peer_cancelled(r_id):
                return self._outcome(
                    ok=False,
                    final_output=self._joined(),
                    failure_mode=FailureMode.NONE,
                    rounds=rounds,
                    results=results,
                    reason=f"peer_cancelled:{r_id}",
                )
            if rb.exhausted_steps(rounds) or rb.exhausted_messages(rounds):
                mode = detect_failure_mode(
                    peer_budget_exhausted=True,
                    rounds=rounds,
                    max_rounds=self.config.max_rounds,
                    rounds_no_progress=no_progress,
                )
                return self._outcome(
                    ok=False,
                    final_output=self._joined(),
                    failure_mode=mode,
                    rounds=rounds,
                    results=results,
                    reason="reviewer_budget_exhausted",
                )
            r_out = await self._call(
                rev,
                goal=goal,
                proposal=p_msg.content,
                context=self._joined(),
                round=rounds,
            )
            r_content = peer_face(r_out, max_chars=self.config.max_result_chars)
            r_msg = PeerMessage(
                sender=r_id,
                receiver=p_id,
                role=AgentRole.REVIEWER,
                content=r_content,
            )
            entered_r = self._record(r_msg)
            if not entered_r:
                zero_inc_msgs += 1
            if isinstance(r_out, PeerResult):
                r_out.messages_sent = r_out.messages_sent + (r_msg,)
                results.append(r_out)
            decision = _extract_decision(r_out, default="approve")
            echo = information_increment(r_content, p_msg.content)["overlap_ratio"]

            # -- 收敛 / 失败判定 --
            if decision in ("approve", "accept", "ok") and accept in (
                "accept",
                "accept_final",
            ):
                return self._outcome(
                    ok=True,
                    final_output=p_msg.content,
                    failure_mode=FailureMode.NONE,
                    rounds=rounds,
                    results=results,
                    reason="converged",
                )
            if decision in ("reject",) and accept in ("reject", "rejected"):
                # 双方都说不行 → 无进展
                no_progress += 1
            if decision == "reject" and accept in ("accept", "accept_final"):
                contradiction = True
                if self.config.stop_on_contradiction:
                    mode = detect_failure_mode(
                        contradiction=True,
                        rounds=rounds,
                        max_rounds=self.config.max_rounds,
                    )
                    return self._outcome(
                        ok=False,
                        final_output=p_msg.content,
                        failure_mode=mode,
                        rounds=rounds,
                        results=results,
                        reason="proposer_accept_reviewer_reject",
                    )
            if no_progress >= 3:
                mode = detect_failure_mode(
                    rounds_no_progress=no_progress,
                    messages_with_zero_increment=zero_inc_msgs,
                    echo_overlap=echo,
                    rounds=rounds,
                    max_rounds=self.config.max_rounds,
                )
                return self._outcome(
                    ok=False,
                    final_output=self._joined(),
                    failure_mode=mode,
                    rounds=rounds,
                    results=results,
                    reason="no_progress",
                )

        mode = detect_failure_mode(
            contradiction=contradiction,
            rounds=self.config.max_rounds,
            rounds_no_progress=no_progress,
            messages_with_zero_increment=zero_inc_msgs,
            # Phase 25 修复：此前漏传 echo_overlap（恒 0）——轮次耗尽时
            # echo_chamber 永远退化成 livelock，六模式之三形同虚设。
            echo_overlap=echo,
            max_rounds=self.config.max_rounds,
            escalation_depth=depth,
            max_escalation_depth=self.config.max_escalation_depth,
        )
        if mode is FailureMode.NONE and no_progress > 0:
            mode = FailureMode.DEADLOCK
        reason = (
            "contradiction_unresolved"
            if contradiction and mode is FailureMode.CONTRADICTION
            else "max_rounds_exceeded"
        )
        return self._outcome(
            ok=False,
            final_output=self._joined(),
            failure_mode=mode,
            rounds=rounds,
            results=results,
            reason=reason,
        )

    async def escalate(self, goal: str, *, depth: int = 0) -> ManagerOutcome:
        """再委托：深度超限 → INFINITE_ESCALATION（独立于主 run）。"""
        if depth > self.config.max_escalation_depth:
            return ManagerOutcome(
                ok=False,
                final_output="",
                failure_mode=FailureMode.INFINITE_ESCALATION,
                rounds=0,
                reason=f"depth {depth} > max {self.config.max_escalation_depth}",
            )
        return await self.run(goal, depth=depth)

    def _joined(self) -> str:
        return "\n".join(self.blackboard)

    @staticmethod
    async def _call(fn: Callable[..., Any], **kwargs: Any) -> Any:
        out = fn(**kwargs)
        if hasattr(out, "__await__"):
            out = await out
        return out


def _extract_decision(result: Any, *, default: str) -> str:
    if isinstance(result, PeerResult):
        meta = result.meta or {}
        for key in ("decision", "action", "verdict"):
            if key in meta:
                return str(meta[key]).lower()
        text = (result.output or "").strip().lower()
    else:
        text = str(result or "").strip().lower()
    for token in ("accept_final", "accept", "approve", "reject", "rejected", "ok"):
        if text.startswith(token) or f" {token}" in text[:40]:
            return token
    return default
