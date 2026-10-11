"""统一 ToolExecutor（R3-2，Agent Runtime 架构文档 §19/§20）。

所有工具调用必须经过统一 Executor，内部执行链::

    Permission → Risk → Timeout → Retry → Execution → Result → Logging → Trace

而不是 Agent 直接 ``neo4j_driver.run(...)``。

设计约束（Strangler 迁移期，文档 §98）：
- 工具**实现**暂不搬家——既有 KG 节点/SearchTool 保持原样，通过
  :meth:`ToolExecutor.register_handler` 把可调用体登记进来即可获得
  统一的超时/重试/权限/风险/轨迹治理；
- 工具**声明**复用 :class:`~agentix.tools.definitions.ToolSpec`
  （R3-2 已扩展 timeout_ms/retryable/max_retries/capabilities/side_effect）；
- 与 LangChain ``bind_tools`` / PydanticToolsParser 的 LLM 选择链路解耦：
  Executor 管执行治理，不管 schema 暴露（文档 §17 的选择过滤属后续 Phase）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from agentix.tools.definitions import RISK_LEVELS, ToolSpec, registry as tool_registry
from agentix.tools.gateway.human_review import (
    ReviewRequest,
    ReviewRequired,
    decision_surface_active,
    declared_level_for,
    effective_risk_level,
    rank_of,
)
from agentix.config import settings

logger = logging.getLogger(__name__)


def _run_remaining_ms() -> float | None:
    """当前 run 的剩余预算毫秒（RequestScope.deadline）；无 scope/deadline → None。"""
    try:
        from agentix.application.services.request_scope import get_current_scope

        scope = get_current_scope()
        if scope is None:
            return None
        return scope.remaining_ms()
    except Exception:  # noqa: BLE001 - scope 故障回原行为（spec 超时）
        return None


def _effective_timeout_ms(spec_timeout_ms: int) -> int:
    """Phase H · W3：工具超时 = min(spec, 剩余 run 预算)；flag 关/无
    deadline → spec 原值（错误文案字节等价）。下限 1ms（预算耗尽即近速失败）。"""
    if not bool(getattr(settings, "ADAPTIVE_TOOL_TIMEOUT_ENABLED", False)):
        return int(spec_timeout_ms)
    remaining = _run_remaining_ms()
    if remaining is None:
        return int(spec_timeout_ms)
    return max(1, min(int(spec_timeout_ms), int(remaining)))


#: 工具处理器签名：``handler(args: dict) -> Any``（同步或 async 均可）
Handler = Callable[[dict], Awaitable[Any]] | Callable[[dict], Any]

#: 审批拦截钩子签名（Phase 16 HITL 桥）：``(request, raw_args) -> awaitable``
ReviewBlockedHook = Callable[[ReviewRequest, dict], Awaitable[Any]]

#: 观测钩子签名（Phase 21 生命周期旁路）：能力层只暴露中性回调，
#: **不认识**事件名（on_tool_before/after 归 agent 侧语义，见
#: ``agentix.agent.hooks.install``——与 Phase 16 组合根同款分层）。
BeforeHook = Callable[[str, dict], None]  # (tool_name, arguments)
AfterHook = Callable[[str, "ToolResult | None"], None]  # (tool_name, result)

#: 钩子超时（秒）：桥挂起动作（Sidecar/breakpoint）不得拖垮工具执行链；
#: 超时按告警处理，``review_required`` 结果不受影响（fail-closed 不变）。
_REVIEW_HOOK_TIMEOUT_S = 2.0


@dataclass
class ToolResult:
    """统一工具返回协议（文档 §20）——Neo4j/MySQL/Milvus/Search/Vision 同构。"""

    tool_name: str
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    success: bool = False
    data: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    citations: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: int = 0
    retryable: bool = False
    trace_id: str | None = None


def _risk_rank(level: str) -> int:
    """工具声明级（``L0``–``L3``）序号。

    保留给按**声明级**排序的场景（如工具选择过滤）；**执行点判定不要用它**
    ——执行点必须走 :func:`effective_risk_level`，否则参数提权会被漏掉。
    """
    return RISK_LEVELS.index(level)


class ToolExecutor:
    """统一工具执行器：**有效风险 → 权限 → 人工审批** → 超时 → 重试 → 执行 → 轨迹。

    - ``permission_checker``（可选）：``(tool_name, spec, policy) -> bool``，
      返回 False 时拒绝执行（错误语义 = 权限不足，不可重试）；
      对应文档 §17 "LLM 不应该看到它没有权限使用的 Tool" 的执行侧兜底。
    - ``policy``：轻量上下文，至少支持 ``max_risk_level``（超出直接拒绝）。
      判定用的是**有效风险等级**（声明级 ∪ 名称级 ∪ 参数提权），不是声明级。
    - ``review_gate``（可选）：人工审批闸门。缺省时惰性取全局单例
      （由 ``settings.HUMAN_REVIEW_ENABLED`` 决定开关），便于测试注入。
    - ``review_blocked_hook``（可选，Phase 16）：拦下后把 ``(request, args)``
      喂给 HITL 桥（挂 Sidecar / breakpoint）。缺省 ``None`` = 零变化。
    """

    def __init__(
        self,
        *,
        permission_checker: Callable[[str, ToolSpec, Any], bool] | None = None,
        policy: Any = None,
        review_gate: Any = None,
        review_blocked_hook: ReviewBlockedHook | None = None,
    ):
        self._handlers: dict[str, Handler] = {}
        self._permission_checker = permission_checker
        self.policy = policy
        #: 人工审批闸门（缺省时在执行点惰性取全局单例，便于测试注入与
        #: ``settings.HUMAN_REVIEW_ENABLED`` 热切换）
        self._review_gate = review_gate
        #: 审批拦截钩子（Phase 16 HITL 桥）。缺省 ``None`` = 零变化路径：
        #: 拦下只落 ``reason="review_required"`` 结果，不触发任何桥动作。
        self._review_blocked_hook = review_blocked_hook
        #: 生命周期观测钩子（Phase 21）。缺省 ``None`` = 零开销路径
        #: （before/after 一次都不调）；由组合根 ``install_tool_hooks`` 注入。
        self._hook_before: BeforeHook | None = None
        self._hook_after: AfterHook | None = None

    def set_review_blocked_hook(self, hook: ReviewBlockedHook | None) -> None:
        """安装/卸载「拦下后喂审批桥」钩子（Phase 16；``None`` = 卸载）。"""
        self._review_blocked_hook = hook

    def set_observation_hooks(
        self,
        *,
        before: BeforeHook | None = None,
        after: AfterHook | None = None,
    ) -> None:
        """安装/卸载生命周期观测钩子（Phase 21；覆盖式，``None`` = 卸载）。

        与 HITL 拦截钩子**正交且有序**：``before`` 在风险/权限/HITL 全部
        闸门放行之后触发，``after`` 在执行结果出口触发——gate 在前、观测
        在后；被闸拒的尝试不触发观测（不是一次执行）。钩子故障自吞告警，
        永不改变 ToolResult。
        """
        self._hook_before = before
        self._hook_after = after

    def _observe_before(self, tool_name: str, arguments: dict[str, Any] | None) -> None:
        """触发 before 观测（未安装即返；故障自吞——永不影响主路径）。"""
        if self._hook_before is None:
            return
        try:
            self._hook_before(tool_name, dict(arguments or {}))
        except Exception as exc:  # noqa: BLE001 - 观测旁路故障不得改变执行结果
            logger.warning("observation before-hook failed for %s: %s", tool_name, exc)

    def _observe_after(self, tool_name: str, result: "ToolResult | None") -> None:
        """触发 after 观测（finally 语义；``result=None`` = 意外逸出路径）。"""
        if self._hook_after is None:
            return
        try:
            self._hook_after(tool_name, result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("observation after-hook failed for %s: %s", tool_name, exc)

    # -- 注册 ------------------------------------------------------------

    def register_handler(self, tool_name: str, handler: Handler, *, spec: ToolSpec | None = None) -> None:
        """登记工具实现；``spec`` 缺省时要求声明式 registry 已有同名 spec。"""
        if spec is not None and not tool_registry.has_tool(tool_name):
            tool_registry.register(spec, lambda: None)
        self._handlers[tool_name] = handler

    def has_handler(self, tool_name: str) -> bool:
        return tool_name in self._handlers

    def handler_names(self) -> tuple[str, ...]:
        """已注册 handler 名（注册表对账/诊断用只读视图，确定性排序）。"""
        return tuple(sorted(self._handlers))

    # -- 执行 ------------------------------------------------------------

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        policy: Any = None,
    ) -> ToolResult:
        """执行一次工具调用（永不抛**业务**异常：失败统一落 :class:`ToolResult`）。

        执行链：**有效风险 → 权限 → 人工审批 → 超时 → 重试 → 执行 → 轨迹**。

        **契约边界（P2 澄清）**：唯一允许向外传播的是 ``asyncio.CancelledError``
        ——取消语义必须上抛（吞掉取消会让调用方的 ``wait_for``/任务树失去响应，
        是更严重的违规）；非 mapping 入参与（flag-on 时）非 JSON 返回值都由
        本方法**转为 failed 结果**，不外抛。

        **有效风险等级**（本方法的核心修正）：此前这里只比较
        ``spec.risk_level``（声明级），而声明的粒度是"这个工具天生多危险"，
        对"这一次调用多危险"一无所知 —— 于是
        ``read_table({'all': True})`` 与 ``read_table({})`` 拿到同一个等级，
        ``max_risk_level`` 策略对**参数提权**完全失明。现在改为
        ``max(声明级, 名称级, 参数提权)``，两层一起算。

        **人工审批在本方法内完成**（调用点级闸门）：审批的判定需要
        ``args``，而本方法是全项目唯一能同时拿到 ``tool_name`` 与
        ``arguments`` 的地方。此前闸门挂在 ``get_tool()``（工具**实例化**
        点），那里只有名字，因此 ``force`` / ``all=true`` 这类参数提权
        永不触发。批准记录也在本方法被消费 —— **一次批准 = 一次执行**，
        而实例化点只做非消费式预检（见 ``tools/__init__.py``）。

        **观测钩子（Phase 21）**：``before`` 在**全部闸门（风险/权限/HITL）
        放行之后**、真正开始执行前触发；``after`` 在两处执行结果出口触发
        ——闸门在前、观测在后（与 ``install_executor_hook`` 正交，不参与
        审批判定；被拒的尝试不触发观测，那不是一次执行）；钩子故障自吞
        告警、永不改变 :class:`ToolResult`（见 ``set_observation_hooks``）。
        """
        started = time.perf_counter()
        call_id = uuid.uuid4().hex[:12]
        try:
            args = dict(arguments or {})
        except (TypeError, ValueError) as exc:
            # P2 契约修正：非 mapping 入参是调用方编程错误，但 execute 承诺
            # 不外抛业务异常（仅 CancelledError 例外）——落 failed 而非打穿调用栈。
            return self._failed(
                tool_name, call_id, started,
                f"invalid arguments (expected mapping): {type(exc).__name__}",
                retryable=False, metadata={"reason": "invalid_arguments"},
            )
        # 1. 解析 spec 与 handler
        spec = tool_registry.get_spec(tool_name) if tool_registry.has_tool(tool_name) else None
        handler = self._handlers.get(tool_name)
        if spec is None or handler is None:
            return self._failed(tool_name, call_id, started, f"unknown tool: {tool_name}", retryable=False)

        active_policy = policy if policy is not None else self.policy

        # 1.5 护栏前置（P4-4.1，TOOL_GUARDRAILS_ENABLED 默认关 ⇒ 零调用）：
        # halt 拒该工具全部调用、block 拒同参再执行——在风险闸门前判，
        # 拦截原因（护栏）优先于风险/权限暴露给调用方（可执行性 ≠ 危险性）。
        if bool(getattr(settings, "TOOL_GUARDRAILS_ENABLED", False)):
            from agentix.policies.guardrails.guardrails import get_tool_guardrails

            guardrail_verdict = get_tool_guardrails().check(tool_name, args)
            if guardrail_verdict is not None and guardrail_verdict.rejected:
                logger.warning(
                    "tool %s rejected by guardrail (%s)",
                    tool_name, guardrail_verdict.tier.value,
                )
                return self._failed(
                    tool_name, call_id, started,
                    f"guardrail {guardrail_verdict.tier.value}: "
                    f"{guardrail_verdict.advice}",
                    retryable=False,
                    metadata={**guardrail_verdict.to_dict(), "reason": "guardrail_rejected"},
                )

        # 2. 有效风险等级（声明级 ∪ 名称级 ∪ 参数提权）
        review_level = effective_risk_level(
            tool_name, args, declared_risk_level=spec.risk_level
        )
        effective_rank = rank_of(review_level)

        # 3. 风险闸门：有效等级超出策略允许上限 → 拒绝
        max_risk = getattr(active_policy, "max_risk_level", None)
        if max_risk is not None and effective_rank > rank_of(declared_level_for(max_risk)):
            return self._failed(
                tool_name, call_id, started,
                f"risk level {review_level.value} exceeds policy max {max_risk}",
                retryable=False,
                metadata={
                    "reason": "risk_denied",
                    "risk_level": review_level.value,
                    "declared_risk_level": spec.risk_level,
                },
            )

        # 4. 权限闸门
        if self._permission_checker is not None:
            try:
                allowed = bool(self._permission_checker(tool_name, spec, active_policy))
            except Exception as exc:  # noqa: BLE001 - 权限钩子故障按拒绝处理（fail-closed）
                logger.warning("permission checker failed for %s: %s", tool_name, exc)
                allowed = False
            if not allowed:
                return self._failed(
                    tool_name, call_id, started, f"permission denied: {tool_name}",
                    retryable=False, metadata={"reason": "permission_denied"},
                )

        # 5. 人工审批闸门（fail-closed：未获批准则拒绝执行）
        review_error = await self._review_preflight(tool_name, spec, args, call_id, started)
        if review_error is not None:
            return review_error

        # 观测钩子 before（Phase 21）：闸门全放行、真正执行才观察
        self._observe_before(tool_name, args)

        # 6. 超时 + 重试
        from agentix.reliability.bulkhead import pool_for_tool, pooled_wait_for

        attempts = 1 + (spec.max_retries if spec.retryable else 0)
        last_error: str | None = None
        last_exc: BaseException | None = None
        for attempt in range(attempts):
            timeout_ms = _effective_timeout_ms(spec.timeout_ms)
            pool = pool_for_tool(tool_name)
            try:
                outcome = await pooled_wait_for(
                    self._invoke(handler, args), timeout_ms / 1000, pool
                )
            except asyncio.TimeoutError:
                last_exc = asyncio.TimeoutError()
                last_error = f"timeout after {timeout_ms}ms"
                logger.warning("tool %s timeout (attempt %d/%d)", tool_name, attempt + 1, attempts)
            except Exception as exc:  # noqa: BLE001 - 统一转 ToolResult
                last_exc = exc
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("tool %s failed (attempt %d/%d): %s", tool_name, attempt + 1, attempts, exc)
            else:
                # P2 返回值类型契约（TOOL_RESULT_CONTRACT_ENABLED，默认关 = 原样
                # 透传，与基线逐位等价）。**在 else 分支检查**：契约违规不是
                # handler 故障，不能落回 except 被当成可重试错误（重试无意义）。
                if bool(getattr(settings, "TOOL_RESULT_CONTRACT_ENABLED", False)):
                    try:
                        json.dumps(outcome, ensure_ascii=False)
                    except (TypeError, ValueError) as exc:
                        logger.warning(
                            "tool %s returned non-serializable result (%s)",
                            tool_name, type(exc).__name__,
                        )
                        invalid = self._failed(
                            tool_name, call_id, started,
                            f"tool returned non-serializable result: {type(exc).__name__}",
                            retryable=False, metadata={"reason": "invalid_result"},
                        )
                        self._mirror_trajectory(tool_name, invalid)
                        self._observe_after(tool_name, invalid)
                        return invalid
                latency = int((time.perf_counter() - started) * 1000)
                result = ToolResult(
                    tool_name=tool_name,
                    call_id=call_id,
                    success=True,
                    data=outcome,
                    latency_ms=latency,
                    retryable=spec.retryable,
                    # 有效风险等级入轨迹：事后审计要能回答"这次调用当时被判成
                    # 几级"，只看声明级会漏掉参数提权这一次的实际情况。
                    metadata={
                        "risk_level": review_level.value,
                        "declared_risk_level": spec.risk_level,
                    },
                )
                if attempt > 0:
                    result.metadata["retry_count"] = attempt
                # 护栏成功记账（P4-4.1，flag 关 ⇒ 零调用）：mutation 成功即
                # 清零该工具计数/拦截；只读成功打断同参链 + 幂等哈希计数。
                if bool(getattr(settings, "TOOL_GUARDRAILS_ENABLED", False)):
                    from agentix.policies.guardrails.guardrails import (
    get_tool_guardrails,
)

                    ok_verdict = get_tool_guardrails().record_success(
                        tool_name, args, result=outcome, mutation=spec.side_effect
                    )
                    if ok_verdict.advice:
                        result.metadata["guardrail"] = ok_verdict.to_dict()
                self._mirror_trajectory(tool_name, result)
                self._observe_after(tool_name, result)
                return result
            if attempt < attempts - 1:
                # Phase H · W3（RETRY_BUDGET_ENABLED，flag 关 = 原样退避重试）：
                # ① 分类审计闸——仅瞬时性错误（timeout/429/503/reset）可重试，
                #    鉴权/参数类 4xx 重试只会放大故障；② run 级共享预算扣减，
                #    与 task/retrieval 层共用一个 token 桶（防 3×3×3 放大）。
                if bool(getattr(settings, "RETRY_BUDGET_ENABLED", False)):
                    from agentix.reliability.retry_budget import (
                        ensure_retry_budget,
                        retryable_error,
                    )

                    if last_exc is not None and not retryable_error(last_exc):
                        logger.info(
                            "tool %s not retryable (%s): giving up",
                            tool_name,
                            type(last_exc).__name__,
                        )
                        break
                    if not ensure_retry_budget().try_consume(layer="tool"):
                        logger.warning(
                            "retry budget exhausted for tool %s: giving up",
                            tool_name,
                        )
                        break
                await asyncio.sleep(0.2 * (2**attempt))  # 指数退避

        latency = int((time.perf_counter() - started) * 1000)
        result = ToolResult(
            tool_name=tool_name,
            call_id=call_id,
            success=False,
            error=last_error,
            latency_ms=latency,
            retryable=spec.retryable,
        )
        # 护栏失败记账（P4-4.1，flag 关 ⇒ 零调用）：三计数器分级 + 恢复建议
        # 进 metadata——**error 文本保持原样**（is_retryable / failure_key 指纹
        # 不受建议拼接影响；面向模型的回喂由 task_retry 终局与 replanner
        # rationale 两个出口完成）。
        if bool(getattr(settings, "TOOL_GUARDRAILS_ENABLED", False)):
            from agentix.policies.guardrails.guardrails import get_tool_guardrails

            fail_verdict = get_tool_guardrails().record_failure(
                tool_name, args, error=last_error or ""
            )
            if fail_verdict.advice:
                result.metadata["guardrail"] = fail_verdict.to_dict()
        self._mirror_trajectory(tool_name, result)
        self._observe_after(tool_name, result)
        return result

    # -- 内部 -------------------------------------------------------------

    @staticmethod
    def _hook_approved(outcome: Any) -> bool:
        """钩子是否已**代为批准**（True / 带 ``approved`` 属性的对象）。

        只有 ``True`` 或 ``approved`` 为真的钩子返回值才触发"再闸一次"；
        其余（含 HITL 桥手工挂起路径返回的 ``BridgeSuspend(approved=False)``）
        一律视为未批准，维持 ``review_required``。
        """
        if outcome is True:
            return True
        return bool(getattr(outcome, "approved", False))

    def _resolve_review_gate(self) -> Any:
        """取审批闸门：显式注入优先，否则惰性取全局单例（读一次 settings）。"""
        if self._review_gate is not None:
            return self._review_gate
        from agentix.tools.gateway.human_review import get_review_gate

        return get_review_gate()

    async def _review_preflight(
        self, tool_name: str, spec: ToolSpec, args: dict, call_id: str, started: float
    ) -> ToolResult | None:
        """调用点级人工审批检查。

        返回 ``None`` 表示放行；返回 :class:`ToolResult` 表示**拒绝执行**
        （fail-closed）。未启用审批时零副作用（闸门内部第一句就返回）。

        为什么把审批失败**转成 ToolResult 而不是往外抛**：本方法所在链路的
        契约是"永不抛异常"（``execute`` docstring / 调用方按结果判成败）。
        若在此处放行 ``ReviewRequired``，它会穿过 ``mcp/client.py`` 的调用栈
        变成未捕获异常 —— 也就是"该拦住的动作没被执行，但错误归类成了系统
        故障"。显式落成 ``reason="review_required"`` 的结果，既拦住了动作，
        又让上层能把它当"待审批"而不是"系统坏了"来处理。

        **Phase 16 拦截钩子**：装了 ``review_blocked_hook`` 时，拦下后先把
        ``(request, raw_args)`` 喂给桥（挂 Sidecar / breakpoint），带超时与
        自吞——钩子故障绝不改变 ``review_required`` 的 fail-closed 结果。
        """
        try:
            gate = self._resolve_review_gate()
        except Exception:  # noqa: BLE001 - 取闸门失败也要 fail-closed
            logger.warning("review gate unavailable for %s (fail-closed)", tool_name)
            return self._failed(
                tool_name, call_id, started, "review gate unavailable (fail-closed)",
                retryable=False, metadata={"reason": "review_gate_unavailable"},
            )

        # F1e·A-P1-4：HITL 进环 flag（仅在下面的终局失败处生效；flag-off 时
        # `_blocked` 返回与基线逐字相同的 review_required 结果）。
        in_loop = bool(settings.HITL_IN_LOOP_ENABLED)

        def _blocked(request: ReviewRequest) -> ToolResult:
            """终局失败（fail-closed）。

            flag-on 且**无任何决策通道**（无 approver 且无 HITL 桥决策面）
            ⇒ 沿 sidecar 语义直接 REJECT``(no_approver_wired_fail_closed)``——
            不留永远没人批的 PENDING（A-P1-4②，验收「高风险无 approver 必
            REJECT」）；其余（flag-off / 有 approver / 有决策面）维持
            review_required。A-P1-4③ 裁决顺序
            deny→hook→ask强制→allow 由本函数与上方既有链共同满足：前置
            deny（风险/guardrail 闸在 preflight 之前）→ hook（可代批，代批
            仍须再过 ask 才放行）→ allow（执行点消费批准）。
            """
            if in_loop and gate.approver is None and not decision_surface_active():
                gate.reject(
                    request.request_id,
                    by="sidecar",
                    note="no_approver_wired_fail_closed",
                )
                logger.warning(
                    "tool %s rejected: no approver wired (fail-closed, in-loop)",
                    tool_name,
                )
                return self._failed(
                    tool_name, call_id, started,
                    f"human review rejected: {tool_name} (no approver wired)",
                    retryable=False,
                    metadata={
                        "reason": "no_approver_wired_fail_closed",
                        "request_id": request.request_id,
                        "risk_level": request.risk_level.value,
                    },
                )
            return self._failed(
                tool_name, call_id, started,
                f"human review required: {tool_name} ({request.risk_level.value})",
                retryable=False,
                metadata={
                    "reason": "review_required",
                    "request_id": request.request_id,
                    "risk_level": request.risk_level.value,
                },
            )

        try:
            # consume=True：这里是**执行点**，一次批准精确对应一次执行
            gate.preflight(tool_name, args, declared_level=declared_level_for(spec.risk_level))
        except ReviewRequired as exc:
            request = exc.request
            logger.warning(
                "tool %s blocked pending human review (%s), request_id=%s",
                tool_name, request.risk_level.value, request.request_id,
            )
            hook_approved = False
            if self._review_blocked_hook is not None:
                try:
                    outcome = await asyncio.wait_for(
                        self._review_blocked_hook(request, dict(args)),
                        timeout=_REVIEW_HOOK_TIMEOUT_S,
                    )
                    hook_approved = self._hook_approved(outcome)
                except Exception as hook_exc:  # noqa: BLE001 - 钩子故障不得改判结果
                    logger.warning(
                        "review_blocked_hook failed for %s: %s (result unaffected)",
                        tool_name, hook_exc,
                    )
            if hook_approved:
                # 钩子已代为批准（permission_mode=bypass / accept_edits）：
                # 再闸一次，命中即消费批准——一次批准 = 一次执行；仍不匹配
                # 则维持 fail-closed（批准范围不会被扩大）。
                try:
                    gate.preflight(
                        tool_name, args, declared_level=declared_level_for(spec.risk_level)
                    )
                    return None
                except ReviewRequired as retry_exc:
                    request = retry_exc.request
                except Exception as re_exc:  # noqa: BLE001 - 再闸故障同样 fail-closed
                    logger.warning(
                        "review re-preflight failed for %s: %s (fail-closed)",
                        tool_name, re_exc,
                    )
                    return self._failed(
                        tool_name, call_id, started, "review preflight failed (fail-closed)",
                        retryable=False, metadata={"reason": "review_preflight_error"},
                    )
            return _blocked(request)
        except Exception as exc:  # noqa: BLE001 - 闸门自身故障同样 fail-closed
            logger.warning("review preflight failed for %s: %s (fail-closed)", tool_name, exc)
            return self._failed(
                tool_name, call_id, started, "review preflight failed (fail-closed)",
                retryable=False, metadata={"reason": "review_preflight_error"},
            )
        return None

    @staticmethod
    async def _invoke(handler: Handler, args: dict) -> Any:
        outcome = handler(args)
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        return outcome

    @staticmethod
    def _failed(
        tool_name: str, call_id: str, started: float, error: str, *,
        retryable: bool = False, metadata: dict | None = None,
    ) -> ToolResult:
        return ToolResult(
            tool_name=tool_name,
            call_id=call_id,
            success=False,
            error=error,
            latency_ms=int((time.perf_counter() - started) * 1000),
            retryable=retryable,
            metadata=metadata or {},
        )

    def _mirror_trajectory(self, tool_name: str, result: ToolResult) -> None:
        """best-effort 镜像到观测层 trajectory。"""
        try:
            from agentix.observability.trajectory import current_recorder

            recorder = current_recorder()
            if recorder is None:
                return
            # begin_step 只返回步 dict、不入列；必须接住返回值交给 end_step 入列。
            # （旧写法丢弃返回值再 pop 上一条已完成步 → 新步不进轨迹 + 历史被覆写）
            step = recorder.begin_step(f"tool.{tool_name}")
            recorder.end_step(
                step,
                status="ok" if result.success else "error",
                attributes={
                    "call_id": result.call_id,
                    "latency_ms": result.latency_ms,
                    "error": result.error,
                },
            )
        except Exception:  # noqa: BLE001
            logger.debug("tool trajectory mirror failed", exc_info=True)


#: 进程级单例（工具执行统一走这里；测试用 reset_tool_executor 重建）
_tool_executor: ToolExecutor | None = None


def get_tool_executor() -> ToolExecutor:
    """进程级单例构造点（A2a 三闸有效化）。

    ``TOOL_SELECTION_ENABLED`` 打开时把选择层策略与其执行侧权限兜底
    （``selection.build_tool_policy`` / ``selection.tool_permission_check``）
    一并注入——此前生产路径上 ``policy`` 与 ``permission_checker`` 零注入，
    Gate A/B 形同空转。flag 关（默认）时 ``build_tool_policy()`` 返回
    ``None``，等价于 ``ToolExecutor()`` 零注入基线；注入失败按零注入回落
    （配置错误不打挂单例）。
    """
    global _tool_executor
    if _tool_executor is None:
        policy = None
        permission_checker = None
        try:
            from agentix.tools.selection import (
                build_tool_policy,
                tool_permission_check,
            )

            policy = build_tool_policy()
            if policy is not None:
                permission_checker = tool_permission_check
        except Exception:  # noqa: BLE001 - 注入故障回落零注入基线（不打挂单例）
            logger.warning("tool policy/permission injection failed; baseline wiring", exc_info=True)
            policy = None
            permission_checker = None
        _tool_executor = ToolExecutor(policy=policy, permission_checker=permission_checker)
    return _tool_executor


def reset_tool_executor() -> None:
    global _tool_executor
    _tool_executor = None
