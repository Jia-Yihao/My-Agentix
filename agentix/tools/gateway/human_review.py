"""高风险动作的人工审批旁路（Human-in-the-loop Review Gate）。

《My Agentix_架构优化方案》§9.1 B 组缺口：「高风险动作有独立 Sidecar 或人工审批旁路」
—— 现状：无独立旁路，写操作无审批。

**为什么需要**：读操作出错可以重试；**写操作出错不可逆**（删库、覆盖、
外部副作用）。把「高风险」识别出来并**在动作真正执行前**拦一道，是
Agent 系统里唯一能兜住「模型自信地做错事」的机制。

设计要点：

- **默认关闭**（``settings.HUMAN_REVIEW_ENABLED = False``）：未接线时本模块
  是**直通**（``decide()`` 恒返回 ``APPROVED``），行为与接线前逐位一致。
- **fail-closed**：开启后，若高风险动作**没有**可用的审批人，
  判定为 ``PENDING``（**不放行**）——「没人能批」不等于「可以放行」。
- **风险分级确定性**：显式映射优先，名称模式兜底，最后默认 ``LOW``。
- **有界**：审批记录按条数封顶，防止长跑进程内存泄漏。
- **可审计**：每次判定都产出 ``ReviewRequest``（含理由），可落库/上报。
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional

from loguru import logger

# M0 shim：RiskLevel 规范位已迁 domain/tool/risk.py（对象同一，M6 清尾）
from agentix.domain.tool.risk import RiskLevel

__all__ = [
    "RiskLevel",
    "ReviewDecision",
    "normalize_decision",
    "ReviewRequest",
    "ReviewPolicy",
    "ReviewRequired",
    "DEFAULT_POLICY",
    "HumanReviewGate",
    "classify_risk",
    "declared_level_for",
    "effective_risk_level",
    "rank_of",
    "requires_human_review",
    "preflight_review",
    "get_review_gate",
    "reset_review_gate",
    "decision_surface_active",
    "digest_args",
]

#: 需要审批的风险等级（MEDIUM 及以上）
REVIEW_RISK_LEVELS = frozenset({"medium", "high", "critical"})


#: 人工裁决的等价写法（人工侧可能给字符串、布尔值或 dict）
_APPROVE_WORDS = frozenset({"approve", "approved", "allow", "yes", "true", "ok", "放行", "批准"})
_REJECT_WORDS = frozenset({"reject", "rejected", "deny", "denied", "no", "false", "拒绝", "驳回"})


def normalize_decision(value: Any) -> ReviewDecision:
    """把人工裁决归一为 ``APPROVED`` / ``REJECTED``。

    **fail-closed**：无法识别的裁决（``None`` / 空串 / 看不懂的字符串 / 结构不符的 dict）
    一律判为 ``REJECTED`` —— "没说明白"绝不意味着"放行"。

    :param value: 人工侧经恢复通道传入的裁决值。
    """
    if isinstance(value, dict):
        for key in ("decision", "action", "result"):
            if key in value:
                return normalize_decision(value[key])
        return ReviewDecision.REJECTED
    if isinstance(value, bool):
        return ReviewDecision.APPROVED if value else ReviewDecision.REJECTED
    if isinstance(value, ReviewDecision):
        return value
    if value is None:
        return ReviewDecision.REJECTED
    text = str(value).strip().lower()
    if text in _APPROVE_WORDS:
        return ReviewDecision.APPROVED
    if text in _REJECT_WORDS:
        return ReviewDecision.REJECTED
    return ReviewDecision.REJECTED


class ReviewRequired(RuntimeError):
    """高风险动作在未获人工批准前被调用（**fail-closed**）。

    调用方应捕获本异常，把 ``request`` 交给审批流程，获准后重试。
    """

    def __init__(self, request: "ReviewRequest") -> None:
        super().__init__(
            f"高风险动作需人工审批：{request.tool_name}"
            f"（{request.risk_level.value}）；request_id={request.request_id}"
        )
        self.request = request


class ReviewDecision(str, Enum):
    #: 无需审批（低风险）或未开启审批
    APPROVED = "approved"
    #: 已提交审批，等待人工裁决（**不得继续执行**）
    PENDING = "pending"
    #: 人工拒绝
    REJECTED = "rejected"


#: 名称模式 → 风险等级（**顺序敏感**：先匹配者胜）
_NAME_PATTERNS: List[tuple] = [
    (re.compile(r"(?i)(drop|truncate|delete|purge|wipe|destroy)"), RiskLevel.CRITICAL),
    (re.compile(r"(?i)(write|update|insert|upsert|create|modify|patch)"), RiskLevel.HIGH),
    (re.compile(r"(?i)(execute|exec|run|eval|shell|command)"), RiskLevel.HIGH),
    (re.compile(r"(?i)(import|ingest|sync|migrate|bulk)"), RiskLevel.MEDIUM),
    (re.compile(r"(?i)(send|publish|notify|email|post)"), RiskLevel.MEDIUM),
]


@dataclass(frozen=True)
class ReviewPolicy:
    """审批策略（不可变，便于共享与快照测试）。"""

    #: 工具名 → 显式风险等级（优先级最高）
    explicit: Dict[str, RiskLevel] = field(default_factory=dict)
    #: 默认风险等级（无任何匹配时）
    default_level: RiskLevel = RiskLevel.LOW
    #: 是否启用名称模式兜底
    use_name_patterns: bool = True

    def resolve(self, tool_name: str) -> RiskLevel:
        name = str(tool_name or "")
        explicit = self.explicit.get(name)
        if explicit is not None:
            return explicit
        if self.use_name_patterns:
            for pattern, level in _NAME_PATTERNS:
                if pattern.search(name):
                    return level
        return self.default_level


#: 默认策略：写/删/执行类工具需审批
DEFAULT_POLICY = ReviewPolicy(
    explicit={
        "cypher_query": RiskLevel.LOW,       # 只读查询（写语句由 Cypher 侧守卫拦）
        "text2sql_query": RiskLevel.LOW,
        "search_documents": RiskLevel.LOW,
    },
    default_level=RiskLevel.LOW,
)


def classify_risk(tool_name: str, args: Optional[Dict[str, Any]] = None,
                  *, policy: ReviewPolicy = DEFAULT_POLICY,
                  declared_level: Optional[RiskLevel] = None) -> RiskLevel:
    """判定一次工具调用的**有效**风险等级（确定性）。

    三个输入取**最大值**（只可提权，不可降权）：

    1. ``policy`` 的名称解析（显式表 → 名称模式 → 默认值）；
    2. ``declared_level``：工具**声明**的风险等级（``ToolSpec.risk_level``
       经 :func:`declared_level_for` 映射）。这是"这个工具天生多危险"，
       与"这次调用多危险"是两件事，必须都算进有效等级——否则一个声明为
       ``L2``（需人工确认）的工具，只要名字不命中模式，就会被当成 ``LOW``
       直通，声明形同虚设；
    3. ``args`` 的参数提权（``force`` / ``all=true`` 等）。

    :param declared_level: 工具声明的等级（可选，缺省表示不参与判定）。
    """
    level = policy.resolve(tool_name)
    if declared_level is not None and rank_of(declared_level) > rank_of(level):
        level = declared_level
    args = args or {}
    escalation = _arg_escalation(args)
    if escalation is not None and rank_of(escalation) > rank_of(level):
        return escalation
    return level


#: ``ToolSpec.risk_level``（L 级）→ 审批侧 ``RiskLevel`` 的**规范映射**。
#:
#: 这是两套分级的唯一真源：
#: - L 级（``definitions.RISK_LEVELS``）回答"**这个工具的能力有多危险**"，
#:   是声明期概念，粒度是工具；
#: - 审批级（``RiskLevel``）回答"**这一次调用要不要人批**"，
#:   是执行期概念，粒度是调用。
#: 二者不是重复设计，但**必须有且只有一处映射**，否则"声明 L2"与
#: "判定 high"会在不同代码路径上各算各的，出现声明了却不生效的缝。
_L_TO_RISK: Dict[str, RiskLevel] = {
    "L0": RiskLevel.LOW,
    "L1": RiskLevel.MEDIUM,
    "L2": RiskLevel.HIGH,
    "L3": RiskLevel.CRITICAL,
}


def declared_level_for(risk_level: str) -> RiskLevel:
    """把工具声明级（``L0``–``L3``）映射为审批等级。

    **未知等级 fail-closed 向上**：映射表里查不到的值按 ``HIGH`` 处理
    （"看不懂声明"绝不等价于"这个工具很安全"）。``ToolSpec`` 已保证
    取值合法，此处是针对绕过校验直接构造 spec 的兜底。
    """
    key = str(risk_level or "").strip().upper()
    level = _L_TO_RISK.get(key)
    if level is None:
        logger.warning(
            "Unknown declared risk_level {!r}: treating as HIGH (fail-closed)", risk_level
        )
        return RiskLevel.HIGH
    return level


def effective_risk_level(
    tool_name: str,
    args: Optional[Dict[str, Any]] = None,
    *,
    declared_risk_level: Optional[str] = None,
    policy: ReviewPolicy = DEFAULT_POLICY,
) -> RiskLevel:
    """便捷入口：由工具名 + 参数 + 声明级算出有效等级。"""
    declared = (
        declared_level_for(declared_risk_level)
        if declared_risk_level is not None
        else None
    )
    return classify_risk(tool_name, args, policy=policy, declared_level=declared)


#: 参数名 → 提权等级（键为小写参数名）
_ARG_ESCALATION_RULES: Dict[str, RiskLevel] = {
    "force": RiskLevel.CRITICAL,
    "overwrite": RiskLevel.CRITICAL,
    "hard_delete": RiskLevel.CRITICAL,
    "drop": RiskLevel.CRITICAL,
    "all": RiskLevel.HIGH,
    "truncate": RiskLevel.HIGH,
    "purge": RiskLevel.HIGH,
}


def _arg_escalation(args: Dict[str, Any]) -> Optional[RiskLevel]:
    """参数层面的提权信号。

    **必须取所有命中键中的最高等级**，而不是"先命中的胜"：
    ``{'all': True, 'force': True}`` 与 ``{'force': True, 'all': True}``
    是同一个调用，但 dict 顺序不同——若按遍历顺序提前 return，前者会被
    误判为 HIGH（漏掉 force 的 CRITICAL），造成**同一组参数因书写顺序不同
    得到不同风险等级**。这是安全判定，不能依赖 dict 插入顺序。
    """
    highest: Optional[RiskLevel] = None
    for key, value in args.items():
        level = _ARG_ESCALATION_RULES.get(str(key).lower())
        if level is None or not _truthy(value):
            continue
        if highest is None or _rank(level) > _rank(highest):
            highest = level
    return highest


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def rank_of(level: RiskLevel) -> int:
    """审批等级序号（low=0 … critical=3）。"""
    return {"low": 0, "medium": 1, "high": 2, "critical": 3}[level.value]


#: 历史私有名保留（内部与旧测试引用）
_rank = rank_of


@dataclass
class ReviewRequest:
    """一次待审批请求。"""

    request_id: str
    tool_name: str
    risk_level: RiskLevel
    reason: str
    args_digest: Dict[str, Any] = field(default_factory=dict)
    trace_id: str = "-"
    created_at: float = field(default_factory=time.time)
    decision: ReviewDecision = ReviewDecision.PENDING
    decided_at: Optional[float] = None
    decided_by: Optional[str] = None
    note: str = ""
    #: 是否已被一次实际调用消费（一次批准只放行一次，防「一次批准长期有效」）
    consumed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "tool_name": self.tool_name,
            "risk_level": self.risk_level.value,
            "reason": self.reason,
            "args_digest": self.args_digest,
            "trace_id": self.trace_id,
            "created_at": self.created_at,
            "decision": self.decision.value,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "note": self.note,
            "consumed": self.consumed,
        }


def _digest_args(args: Dict[str, Any], *, max_len: int = 300) -> Dict[str, Any]:
    """参数摘要：只保留键与**短**值，避免把敏感/超大 payload 落进审批记录。"""
    out: Dict[str, Any] = {}
    for key, value in (args or {}).items():
        text = value if isinstance(value, str) else str(value)
        out[str(key)] = text if len(text) <= max_len else text[:max_len] + "…"
    return out


def digest_args(args: Dict[str, Any], *, max_len: int = 300) -> Dict[str, Any]:
    """参数摘要的**公开**入口（算法与 :func:`preflight` 的匹配校验同源）。

    供审批恢复/编辑合并后重算摘要——审批记录的 ``args_digest`` 必须与
    执行点 ``preflight`` 用同一算法生成，否则 CRITICAL 级「摘要逐位相同」
    校验会把人工已批准的编辑后调用再次拦下。
    """
    return _digest_args(args, max_len=max_len)


#: 审批人签名：接收请求，返回裁决
Approver = Callable[[ReviewRequest], Awaitable[ReviewDecision]]


class HumanReviewGate:
    """审批旁路：判定 → 登记 → （可选）等待人工裁决。"""

    def __init__(
        self,
        *,
        enabled: bool = False,
        policy: ReviewPolicy = DEFAULT_POLICY,
        approver: Optional[Approver] = None,
        max_requests: int = 200,
    ) -> None:
        self.enabled = bool(enabled)
        self.policy = policy
        self.approver = approver
        self._requests: Dict[str, ReviewRequest] = {}
        self._order: List[str] = []
        self._max_requests = max(1, int(max_requests))

    # -- 判定 ---------------------------------------------------------------

    def requires_review(self, tool_name: str, args: Optional[Dict[str, Any]] = None,
                        *, declared_level: Optional[RiskLevel] = None) -> bool:
        """该调用是否需要人工审批（未启用时恒 False）。

        ``declared_level`` 参与判定：工具自己声明的等级（``ToolSpec``）
        与调用参数一样，都是"提权信号"。
        """
        if not self.enabled:
            return False
        return (
            classify_risk(tool_name, args, policy=self.policy,
                          declared_level=declared_level).value in REVIEW_RISK_LEVELS
        )

    async def decide(
        self,
        tool_name: str,
        args: Optional[Dict[str, Any]] = None,
        *,
        trace_id: str = "-",
        declared_level: Optional[RiskLevel] = None,
    ) -> tuple:
        """核心入口：返回 ``(decision, request_or_None)``。

        - 未启用 / 低风险 → ``(APPROVED, None)``（直通，零副作用）
        - 高风险 + 有审批人 → 调用审批人；``PENDING`` 时登记请求
        - 高风险 + **无审批人** → ``PENDING``（**fail-closed**，不放行）
        """
        if not self.enabled:
            return ReviewDecision.APPROVED, None

        level = classify_risk(tool_name, args, policy=self.policy,
                              declared_level=declared_level)
        if level.value not in REVIEW_RISK_LEVELS:
            return ReviewDecision.APPROVED, None

        request = ReviewRequest(
            request_id=uuid.uuid4().hex,
            tool_name=str(tool_name),
            risk_level=level,
            reason=f"风险等级 {level.value} 的动作需人工审批",
            args_digest=_digest_args(args or {}),
            trace_id=trace_id,
        )

        if self.approver is None:
            # fail-closed：无人可批 ⇒ 不放行
            self._register(request)
            logger.warning(
                "High-risk tool '{}' blocked: no approver wired (fail-closed)", tool_name
            )
            return ReviewDecision.PENDING, request

        try:
            decision = await self.approver(request)
        except Exception as exc:  # noqa: BLE001 - 审批通道故障必须 fail-closed
            logger.error("Approver failed for '{}': {} (fail-closed)", tool_name, exc)
            self._register(request)
            return ReviewDecision.PENDING, request

        request.decision = decision
        request.decided_at = time.time()
        self._register(request)
        return decision, request

    # -- 人工裁决 -----------------------------------------------------------

    def preflight(
        self,
        tool_name: str,
        args: Optional[Dict[str, Any]] = None,
        *,
        trace_id: str = "-",
        consume: bool = True,
        declared_level: Optional[RiskLevel] = None,
    ) -> Optional[ReviewRequest]:
        """**同步**前置检查（用于工具实例化 / 调用前的硬闸门）。

        通过 → 返回命中的批准记录（或 ``None`` 表示本就无需审批）；
        未通过 → **抛** :class:`ReviewRequired`（fail-closed）。

        「一次批准只放行一次」：批准记录被消费后不可复用，避免一次批准
        在后续所有调用中长期生效。

        **批准范围必须与本次调用一致**（否则等于提权）：匹配条件为
        ``tool_name`` + **本次调用的 risk_level**；当等级为 ``CRITICAL`` 时
        还要求参数摘要逐位相同。仅按工具名匹配是漏洞——先批准一次
        ``import_data({})``（MEDIUM），再用同一次批准去放行
        ``import_data({'all': True, 'force': True})``（CRITICAL），
        就会让"批了一次小操作"变成"放行一次全量强删"。

        :param consume: 命中后是否把该批准记为「已消费」。
            **实例化点必须传 ``False``**：实例化只是"拿到工具对象"，
            不是"执行了一次动作"。若在实例化时就消费掉批准，会出现两种
            坏情形——(a) 一个人批的是"执行一次"，结果被"取对象"吃掉，
            真正执行时又要求重新批；(b) 携带具体参数的批准被一个
            **没有参数**的实例化检查消费掉（``args=None`` 无从校验
            ``args_digest``）。把消费权收归到**执行点**，一次批准才精确
            对应一次动作。
        """
        if not self.enabled:
            return None

        level = classify_risk(tool_name, args, policy=self.policy,
                              declared_level=declared_level)
        if level.value not in REVIEW_RISK_LEVELS:
            return None

        digest = _digest_args(args or {})
        for request_id in reversed(self._order):
            request = self._requests.get(request_id)
            if (
                request is not None
                and request.tool_name == str(tool_name)
                and request.decision == ReviewDecision.APPROVED
                and not request.consumed
                and request.risk_level == level
                and (level is not RiskLevel.CRITICAL or request.args_digest == digest)
            ):
                if consume:
                    request.consumed = True
                return request

        request = ReviewRequest(
            request_id=uuid.uuid4().hex,
            tool_name=str(tool_name),
            risk_level=level,
            reason=f"风险等级 {level.value} 的动作需人工审批（同步前置检查未通过）",
            args_digest=digest,
            trace_id=trace_id,
        )
        self._register(request)
        logger.warning(
            "High-risk tool '{}' blocked by preflight (fail-closed), request_id={}",
            tool_name,
            request.request_id,
        )
        raise ReviewRequired(request)

    def approve(self, request_id: str, *, by: str = "human", note: str = "") -> bool:
        return self._resolve(request_id, ReviewDecision.APPROVED, by=by, note=note)

    def reject(self, request_id: str, *, by: str = "human", note: str = "") -> bool:
        return self._resolve(request_id, ReviewDecision.REJECTED, by=by, note=note)

    def restore_request(self, request: ReviewRequest) -> bool:
        """恢复通道：把一条请求（已决或仍挂起）登记回闸门（进程重启恢复用）。

        本方法**不放行任何东西**——它只是把记录放回簿记；放行与否仍由
        ``preflight`` 的既有匹配规则（tool + risk_level [+ digest] + 未消费）
        决定。同 id 已存在时返回 ``False``（幂等，防止恢复流程覆盖新判定）。
        """
        if request.request_id in self._requests:
            return False
        self._register(request)
        return True

    def _resolve(
        self, request_id: str, decision: ReviewDecision, *, by: str, note: str
    ) -> bool:
        request = self._requests.get(request_id)
        if request is None or request.decision != ReviewDecision.PENDING:
            return False
        request.decision = decision
        request.decided_at = time.time()
        request.decided_by = by
        request.note = note
        return True

    # -- 查询 ---------------------------------------------------------------

    def get(self, request_id: str) -> Optional[ReviewRequest]:
        return self._requests.get(request_id)

    def pending(self) -> List[ReviewRequest]:
        return [
            self._requests[rid]
            for rid in self._order
            if rid in self._requests
            and self._requests[rid].decision == ReviewDecision.PENDING
        ]

    def stats(self) -> Dict[str, Any]:
        by_decision: Dict[str, int] = {}
        for request in self._requests.values():
            by_decision[request.decision.value] = by_decision.get(request.decision.value, 0) + 1
        return {
            "enabled": self.enabled,
            "total": len(self._requests),
            "by_decision": by_decision,
            "pending": len(self.pending()),
        }

    # -- 维护 ---------------------------------------------------------------

    def _register(self, request: ReviewRequest) -> None:
        self._requests[request.request_id] = request
        self._order.append(request.request_id)
        while len(self._order) > self._max_requests:
            oldest = self._order.pop(0)
            self._requests.pop(oldest, None)

    def clear(self) -> None:
        self._requests.clear()
        self._order.clear()


# ---------------------------------------------------------------------------
# 兼容别名 + 单例
# ---------------------------------------------------------------------------


def requires_human_review(
    tool_name: str,
    args: Optional[Dict[str, Any]] = None,
    *,
    gate: Optional[HumanReviewGate] = None,
    declared_risk_level: Optional[str] = None,
) -> bool:
    """模块级便捷判定（历史骨架 API 的兼容入口）。"""
    target = gate if gate is not None else get_review_gate()
    declared = (
        declared_level_for(declared_risk_level)
        if declared_risk_level is not None
        else None
    )
    return target.requires_review(tool_name, args, declared_level=declared)


def preflight_review(
    tool_name: str,
    args: Optional[Dict[str, Any]] = None,
    *,
    gate: Optional[HumanReviewGate] = None,
    consume: bool = True,
    declared_risk_level: Optional[str] = None,
) -> Optional[ReviewRequest]:
    """模块级同步前置检查（未启用时恒返回 ``None``，零副作用）。

    :param consume: 见 :meth:`HumanReviewGate.preflight`——实例化点传 ``False``，
        执行点保持默认 ``True``。
    :param declared_risk_level: 工具声明级（``L0``–``L3``），传入后参与有效
        等级判定，让"声明为 L2 的工具"不必依赖名字命中模式也能被拦下。
    """
    target = gate if gate is not None else get_review_gate()
    declared = (
        declared_level_for(declared_risk_level)
        if declared_risk_level is not None
        else None
    )
    return target.preflight(tool_name, args, consume=consume, declared_level=declared)


_gate: Optional[HumanReviewGate] = None

#: 审批决策面接线标记（A2a）：组合根 ``install_executor_hook`` 在创建全局
#: 闸门**之前**置位——HITL 桥的 ``bridge.decide`` 决策通道就是审批决策面。
#: 置位后 ``get_review_gate`` 不再打「无人可批」的启动期告警（该告警只针对
#: 「开了却没有任何决策通道」的死配置）。``reset_review_gate`` 归零。
_decision_surface: bool = False


def mark_decision_surface() -> None:
    """宣告「审批决策面已接线」（组合根 HITL 桥安装时调用，幂等）。"""
    global _decision_surface
    _decision_surface = True


def clear_decision_surface() -> None:
    """撤销「审批决策面已接线」宣告（桥卸载/测试复位时调用）。"""
    global _decision_surface
    _decision_surface = False


def decision_surface_active() -> bool:
    """审批决策面是否已接线（``mark_decision_surface`` 置位后为 True）。

    F1e·A-P1-4② 的读取点：executor 用它区分「真没人可批」（无 approver
    且无决策面 ⇒ flag-on 时直接 REJECT）与「有 HITL 桥决策面」（照旧
    review_required 挂起/待批）。
    """
    return _decision_surface


def get_review_gate() -> HumanReviewGate:
    """获取全局审批旁路（默认**关闭**，由 settings 决定是否启用）。

    ⚠️ 若要开启 ``HUMAN_REVIEW_ENABLED``，必须先提供审批人（``approver``）
    或决策面（组合根 ``install_executor_hook`` 接线的 HITL 桥，
    ``mark_decision_surface`` 置位）。两者都没有时，所有高风险工具都会稳定
    落到 ``PENDING``（fail-closed）。对大多数调用方而言，这等于「开启即全线
    阻断」：逻辑上最安全，但运维上非常容易误判为"系统坏了"。此处主动打一条
    **启动期告警**，把「已开启 + 无决策通道 ⇒ 高风险工具全部不可用」讲清楚，
    避免静默阻断。
    """
    global _gate
    if _gate is None:
        from agentix.config import settings

        enabled = bool(getattr(settings, "HUMAN_REVIEW_ENABLED", False))
        _gate = HumanReviewGate(enabled=enabled, policy=DEFAULT_POLICY)
        if enabled and _gate.approver is None and not _decision_surface:
            logger.warning(
                "HUMAN_REVIEW_ENABLED=true but no approver is wired: every high-risk "
                "tool will be fail-closed to PENDING and can never execute. Wire an "
                "approver via HumanReviewGate(approver=...) or keep the feature off."
            )
    return _gate


def reset_review_gate() -> None:
    global _gate, _decision_surface
    _gate = None
    _decision_surface = False
