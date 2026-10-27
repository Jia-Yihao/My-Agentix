"""P1 任务执行管线：拆解 → 波次并行 → 治理执行 → 分层校验 → 四终态。

《My-Agentix 项目说明书》岗位对照·任务执行（P1）的执行侧核心。与
``AgentLoop``（L3 harness，通用 ReAct 循环）的分工：

- ``AgentLoop`` 是**通用循环**（planner→executor→verifier 每步由 LLM 决策）；
- 本管线是**声明式任务执行器**——任务先拆解为带依赖的步骤（LLM 拆解或
  调用方注入 ``task_steps``），拓扑排序成波次，**同波并行、波间串行**，
  每步经 ``ToolExecutor`` 治理链路执行（有效风险 → 权限 → HITL → 超时），
  最后用 ``LayeredVerifier`` 分层校验收口为四终态。

四终态与 ``AgentLoop`` 对齐（``LoopResult.terminal`` 同一取值域）：

- ``completed`` —— 校验通过（部分失败但有可用产物 → DEGRADED 也算通过）；
- ``failed`` —— 拆解/计划失败、计划结构非法、校验器自身异常（诚实降级）；
- ``budget_exceeded`` —— 步数超出 ``MAX_ITERATIONS``（执行前检查，含部分波次）；
- ``verify_failed`` —— 校验器判定不通过（如全部步骤失败、答案实质为空）。

**菜单边界（有意为之）**：可拆解的工具仅限 ``ToolExecutor`` 已接管
（``has_handler``）∩ registry 已声明的工具——任务管线**只走治理执行链路**，
不为 langchain 直连工具开口子（绕过闸门 = 治理失效）。flag-off 时本模块
不可达（路由侧门控），组合根也不注册两个写工具。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Dict, List, Optional, Sequence

from loguru import logger
from pydantic import BaseModel, Field

from agentix.orchestration.orchestrator import LoopResult
from agentix.policies.verifier import LayeredVerifier
from agentix.application.services.agent_loop_adapter import build_task_spec
from agentix.application.services.task_plan import (
    PlanError,
    TaskSpec as WaveTask,
    plan_waves,
)
from agentix.application.services.task_retry import (
    DEFAULT_MAX_ATTEMPTS,
    BatchOutcome,
    TaskOutcome,
    run_batch,
)
from agentix.config import settings
from agentix.domain.agent.types import (
    ContextSnapshot,
    FailureLayer,
    Observation,
    Plan,
    PlanStep,
    VerificationResult,
)

__all__ = [
    "TaskStepDecl",
    "TaskDecompose",
    "TaskDecomposeError",
    "TaskStepError",
    "run_task_execution",
]


class TaskDecomposeError(ValueError):
    """任务拆解结果非法（空步骤/超步数/id 冲突/未知工具/依赖引用缺失）。"""


class TaskStepError(RuntimeError):
    """单步执行失败（消息文本携带工具错误，供 ``is_retryable`` 判定重试性）。"""


class TaskStepDecl(BaseModel):
    """LLM 拆解出的单步声明（同时是调用方注入 ``task_steps`` 的形状）。"""

    id: str = Field(description="步骤唯一标识（如 s1/s2，全批唯一）")
    tool: str = Field(description="要调用的工具名（必须来自可用工具清单）")
    args: Dict[str, Any] = Field(default_factory=dict, description="工具入参")
    depends_on: List[str] = Field(
        default_factory=list, description="前置依赖的步骤 id 列表（无依赖为空）"
    )
    rationale: str = Field(default="", description="该步骤为何必要（一句话）")


class TaskDecompose(BaseModel):
    """LLM 拆解结构化输出（``with_structured_output`` 目标类型）。"""

    steps: List[TaskStepDecl]


_DECOMPOSE_SYSTEM_PROMPT = """你是测试数据领域任务编排的拆解器。把用户的任务目标拆成**可执行工具步骤**的有序计划。

可用工具清单（只允许使用清单内工具名）：
{menu}

拆解规则：
1. 每步给出唯一 id（s1、s2、…）、tool、args、depends_on、rationale；
2. depends_on 只能引用同批其他步骤的 id；无依赖留空列表；
3. 步数不超过 {max_steps}，产出最小充分步骤集；无依赖关系的步骤会被**并行**执行；
4. args 必须符合该工具的参数 schema（必填项不可缺；宁可少拆一步，也不产出无法执行的参数）；
5. 任务无法映射到任何工具步骤时，返回空 steps 列表（诚实失败，不硬凑）。
"""


def _executable_menu() -> Dict[str, Any]:
    """可执行工具菜单：``ToolExecutor`` 已接管 ∩ registry 已声明。

    返回 ``{tool_name: ToolSpec}``。只含经治理执行链路（风险/权限/HITL）
    的工具——任务管线不绕闸（见模块 docstring 的菜单边界）。
    """
    from agentix.tools.definitions import registry as tool_registry
    from agentix.tools.gateway.executor import get_tool_executor

    executor = get_tool_executor()
    menu: Dict[str, Any] = {}
    for spec in tool_registry.get_specs():
        if executor.has_handler(spec.name):
            menu[spec.name] = spec
    return menu


def _format_menu(menu: Dict[str, Any]) -> str:
    """菜单 → 提示词段（描述首行 + 参数 schema JSON）。"""
    lines = []
    for name, spec in sorted(menu.items()):
        desc = (spec.description or "").splitlines()
        first = desc[0] if desc else ""
        schema = json.dumps(spec.parameters, ensure_ascii=False)
        lines.append(f"- {name}: {first}\n  参数 schema: {schema}")
    return "\n".join(lines) if lines else "（当前无可用工具）"


async def _llm_decompose(
    message: str, menu: Dict[str, Any], max_steps: int
) -> List[TaskStepDecl]:
    """LLM 拆解（temperature=0 + 结构化输出）。测试可 monkeypatch 本函数。"""
    from agentix.application.agents.llm_factory import get_llm_client

    model = get_llm_client(temperature=0.0, tags=["task_decompose"], role="planner")
    system = _DECOMPOSE_SYSTEM_PROMPT.format(
        menu=_format_menu(menu), max_steps=max_steps
    )
    response = await model.with_structured_output(TaskDecompose).ainvoke(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": message},
        ]
    )
    return list(response.steps)


def _normalize_steps(
    raw: Sequence[Any], *, menu: Dict[str, Any]
) -> List[TaskStepDecl]:
    """把注入/LLM 产出的步骤规整为声明列表；非法即抛 ``TaskDecomposeError``。

    **不按 ``MAX_ITERATIONS`` 截断步数**：超出预算的计划交给执行侧的
    ``budget_exceeded`` 终态（与 AgentLoop 语义一致——预算在执行前检查，
    而非在拆解期拒绝），过长计划因此是可解释的预算失败而非拆解失败。
    """
    decls: List[TaskStepDecl] = []
    for entry in raw or []:
        if isinstance(entry, TaskStepDecl):
            decls.append(entry)
        else:
            try:
                decls.append(TaskStepDecl.model_validate(entry))
            except Exception as exc:  # noqa: BLE001 - 拆解失败转数据
                raise TaskDecomposeError(f"invalid step payload: {exc}") from exc

    if not decls:
        raise TaskDecomposeError("decompose produced no steps")

    ids: List[str] = []
    for decl in decls:
        step_id = (decl.id or "").strip()
        if not step_id:
            raise TaskDecomposeError("step id must be non-empty")
        if step_id in ids:
            raise TaskDecomposeError(f"duplicate step id: {step_id}")
        ids.append(step_id)
        decl.id = step_id
        tool = (decl.tool or "").strip()
        if not tool:
            raise TaskDecomposeError(f"step {step_id}: tool must be non-empty")
        if tool not in menu:
            raise TaskDecomposeError(
                f"step {step_id}: unknown or ungoverned tool {tool!r} "
                f"(allowed: {sorted(menu)})"
            )
        decl.tool = tool

    known = set(ids)
    for decl in decls:
        for dep in decl.depends_on:
            if dep == decl.id:
                raise TaskDecomposeError(f"step {decl.id}: self dependency")
            if dep not in known:
                raise TaskDecomposeError(
                    f"step {decl.id}: depends on unknown step {dep!r}"
                )
    return decls


def _assign_step_keys(decls: Sequence[TaskStepDecl]) -> Dict[str, str]:
    """步骤身份 → ``decl.id`` 的映射（LG-3-4 恢复期幂等）。

    身份**不取**模型自拟的 ``id``：恢复会重新拆解，id 与顺序都不保证复现，
    按 id 记完成清单等于让清单在恢复后全部失配。改取 (tool, 规范化 args) 的
    指纹；同一计划里完全相同的步骤按出现序号区分，不把两次合法调用并成一次。
    """
    seen: Dict[str, int] = {}
    keys: Dict[str, str] = {}
    for decl in decls:
        payload = json.dumps(
            {"tool": decl.tool, "args": decl.args},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
        occurrence = seen.get(fingerprint, 0)
        seen[fingerprint] = occurrence + 1
        keys[decl.id] = f"{fingerprint}:{occurrence}"
    return keys


def _open_step_ledger(session_id: str, step_scope: str) -> Optional[Any]:
    """LG-3-4：开关 + 键齐备才开清单；store 构造不连库（惰性 engine）。"""
    if not bool(getattr(settings, "RECOVERY_STEP_LEDGER_ENABLED", False)):
        return None
    if not session_id or not step_scope:
        return None
    from agentix.infrastructure.persistence.step_ledger_store import (
        get_step_ledger_store,
    )

    return get_step_ledger_store()


def _content_text(value: Any, *, tool: str) -> str:
    """工具返回值 → 观测正文（保证 ≥ MIN_ANSWER_CHARS，防空答案误判 INCOMPLETE）。"""
    if isinstance(value, str):
        text = value.strip()
    elif value is None:
        text = ""
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):  # pragma: no cover - 默认=str 兜底
            text = str(value)
    if len(text) < 6:
        text = f"{tool}: ok ({len(text)} chars)"
    return text


def _to_observation(spec: WaveTask, outcome: TaskOutcome, *, wave: int) -> Observation:
    """``TaskOutcome`` → 领域观测（失败是数据：error_code 固定 tool_exec_failed）。"""
    tool = str(spec.payload.get("tool") or spec.name)
    meta = {"tool": tool, "attempts": outcome.attempts, "wave": wave}
    if outcome.ok:
        content = _content_text(outcome.value, tool=tool)
        # Stage F2b-d（R-P2-3 证据池）：成功执行结果统一进观测 meta["evidence"]
        # （content ≤4000 + source），供分层验证器 L2 sources / L3 grounding
        # 消费；默认关 ⇒ meta 无 evidence 键，与基线逐位等价。证据取原始
        # 正文（在去重 stub 之前），grounding 不吃折叠占位文本。
        if bool(getattr(settings, "EVIDENCE_POOL_ENABLED", False)):
            meta["evidence"] = [{"content": content[:4000], "source": tool}]
        # P4-4.2：重复结果 stub（TOOL_GUARDRAILS_ENABLED 默认关 ⇒ 零调用）——
        # 成功值已产出（治理链/校验可用），只折叠进入观测正文的表示；
        # 第 2 次字节级重复起换 stub，轮询不豁免。
        if bool(getattr(settings, "TOOL_GUARDRAILS_ENABLED", False)):
            from agentix.tools.gateway.result_limiter import (
    get_result_deduplicator,
)

            stub = get_result_deduplicator().observe(tool, outcome.value)
            if stub is not None:
                meta["deduped"] = True
                content = stub
        return Observation(
            step_id=spec.name,
            content=content,
            ok=True,
            meta=meta,
        )
    return Observation(
        step_id=spec.name,
        content=(outcome.error or f"tool failed: {tool}")[:400],
        ok=False,
        failure_layer=FailureLayer.TOOL,
        # L2 排除 tool_exec_failed（失败不冒充未消解 error）；全失败由 L1 收口
        error_code="tool_exec_failed",
        meta=meta,
    )


def _synthesize(observations: Sequence[Observation]) -> str:
    """观测 → 面向用户的可读回执（成功/失败逐步列出，诚实呈现）。"""
    if not observations:
        return "任务未执行任何步骤。"
    ok_count = sum(1 for o in observations if o.ok)
    lines = [f"任务执行结果：{ok_count}/{len(observations)} 步成功"]
    for obs in observations:
        mark = "✓" if obs.ok else "✗"
        tool = obs.meta.get("tool", "")
        tail = obs.content.replace("\n", " ")[:200]
        lines.append(f"{mark} {obs.step_id}（{tool}）：{tail}")
    return "\n".join(lines)


def _make_result(
    *,
    task_id: str,
    final: str,
    passed: bool,
    plan: Plan,
    observations: Sequence[Observation],
    reason: str,
    terminal: str,
    steps_used: int,
    verification: Optional[VerificationResult] = None,
) -> LoopResult:
    return LoopResult(
        task_id=task_id,
        final=final,
        passed=passed,
        plan=plan,
        context=ContextSnapshot(prompt=""),
        observations=tuple(observations),
        verification=verification,
        reason=reason,
        terminal=terminal,
        steps_used=steps_used,
        replans=0,
    )


async def run_task_execution(
    message: str,
    *,
    session_id: str = "",
    task_steps: Optional[Sequence[Any]] = None,
    route: str = "task-query",
    review_interrupt: Optional[Any] = None,
    step_scope: str = "",
) -> LoopResult:
    """执行一次任务：拆解 → 波次 → ToolExecutor 治理执行 → LayeredVerifier。

    :param message: 用户任务目标（domain ``TaskSpec.goal``）。
    :param session_id: 会话 id（进 ``task_id``，可为空）。
    :param task_steps: 声明式步骤注入（测试/调用方给定）。``None`` 走 LLM 拆解。
    :param route: 透传 ``LayeredVerifier``（``task-query`` 不在 CITATION_ROUTES）。
    :param review_interrupt: F1e·A-P1-4 进环中断回调（lg_builder 注入壳经
        ``execute_task`` 透传；``None`` = 现行为 review_required 快速失败）。
        挂起/恢复语义见下方 ``_unit`` 与波循环注释。
    :param step_scope: LG-3-4 节点执行作用域（``execute_task`` 取 LangGraph
        ``checkpoint_ns``）。与 ``RECOVERY_STEP_LEDGER_ENABLED`` 同时具备时，
        已完成步骤落 ``run_step_ledger`` 并在同作用域重入时跳过；缺一 = 不开清单。
    """
    if not settings.TASK_EXEC_ENABLED:
        task = build_task_spec(message, session_id=session_id)
        return _make_result(
            task_id=task.task_id,
            final="任务执行未启用（TASK_EXEC_ENABLED=false）。",
            passed=False,
            plan=Plan(),
            observations=(),
            reason="task_exec_disabled",
            terminal="failed",
            steps_used=0,
        )

    task = build_task_spec(message, session_id=session_id)
    max_steps = max(1, int(settings.MAX_ITERATIONS))

    # ---- 1. 拆解（注入优先；否则 LLM，菜单为空诚实失败）------------------
    try:
        menu = _executable_menu()
        if not menu:
            raise TaskDecomposeError("no executable tools registered")
        if task_steps is not None:
            decls = _normalize_steps(task_steps, menu=menu)
        else:
            raw = await _llm_decompose(message, menu, max_steps)
            decls = _normalize_steps(raw, menu=menu)
    except Exception as exc:  # noqa: BLE001 - 拆解失败 = 数据（terminal failed）
        reason = f"decompose_failed: {type(exc).__name__}: {exc}"
        logger.warning("task decompose failed: {}", reason)
        return _make_result(
            task_id=task.task_id,
            final=f"任务拆解失败：{exc}",
            passed=False,
            plan=Plan(),
            observations=(),
            reason=reason,
            terminal="failed",
            steps_used=0,
        )

    # ---- 1.5 恢复期幂等：读本作用域已完成步骤（LG-3-4）-------------------
    ledger = _open_step_ledger(session_id, step_scope)
    step_keys: Dict[str, str] = {}
    done_steps: Dict[str, Dict[str, Any]] = {}
    if ledger is not None:
        step_keys = _assign_step_keys(decls)
        try:
            done_steps = await asyncio.to_thread(
                ledger.load_scope, session_id, step_scope
            )
        except Exception as exc:  # noqa: BLE001 - 辅助子系统故障只降级（纪律 2）
            logger.warning(
                "step ledger load failed; running without replay protection: {}", exc
            )
            ledger = None
            done_steps = {}

    # ---- 2. 计划（拓扑波次 + 领域 Plan）----------------------------------
    wave_specs = [
        WaveTask(
            name=decl.id,
            depends_on=tuple(decl.depends_on),
            payload={"tool": decl.tool, "args": dict(decl.args)},
            meta={"rationale": decl.rationale},
        )
        for decl in decls
    ]
    try:
        waves = plan_waves(wave_specs, strict=True)
    except PlanError as exc:
        reason = f"plan_failed: {exc}"
        logger.warning("task plan failed: {}", reason)
        return _make_result(
            task_id=task.task_id,
            final=f"任务计划非法：{exc}",
            passed=False,
            plan=Plan(),
            observations=(),
            reason=reason,
            terminal="failed",
            steps_used=0,
        )

    plan = Plan(
        steps=tuple(
            PlanStep(
                id=decl.id,
                action=decl.tool,
                depends_on=tuple(decl.depends_on),
                tool=decl.tool,
                rationale=decl.rationale,
            )
            for decl in decls
        ),
        planner="task_decompose",
    )

    # ---- 3. 逐波执行（预算前置检查；波内并行、波间串行）-----------------
    from agentix.tools.gateway.executor import get_tool_executor

    executor = get_tool_executor()

    # F1e·A-P1-4：本轮首个 review_required 的挂起载荷。GraphInterrupt 必须
    # 在 run_batch **之外**抛出——task_retry.run_task 把 BaseException 转成
    # TaskOutcome（除 CancelledError 外一律吞），_unit 内直接调 interrupt
    # 会被吃掉，故先暂存、批结束后在本节点内调用。
    pending: Optional[Dict[str, Any]] = None

    async def _unit(spec: WaveTask) -> Any:
        nonlocal pending
        tool = str(spec.payload.get("tool") or "")
        args = dict(spec.payload.get("args") or {})
        result = await executor.execute(tool, args)
        if not result.success:
            # 消息携带工具错误原文 → run_task 的 is_retryable 决定是否重试：
            # timeout/连接类 → 重试；invalid_request/HITL 拒绝 → 快速失败。
            md = result.metadata if isinstance(result.metadata, dict) else {}
            if (
                review_interrupt is not None
                and pending is None
                and md.get("reason") == "review_required"
            ):
                pending = {
                    "spec": spec,
                    "tool": tool,
                    "args": args,
                    "error": result.error or f"tool failed: {tool}",
                    "request_id": md.get("request_id"),
                    "risk_level": md.get("risk_level"),
                    "session_id": session_id,
                }
            raise TaskStepError(result.error or f"tool failed: {tool}")
        return result.data

    observations: List[Observation] = []
    steps_used = 0
    total_attempts = 0
    executed_waves = 0

    def _budget_result() -> LoopResult:
        return _make_result(
            task_id=task.task_id,
            final=_synthesize(observations),
            passed=False,
            plan=plan,
            observations=observations,
            reason=f"budget_exceeded steps={steps_used}",
            terminal="budget_exceeded",
            steps_used=steps_used,
        )

    for wave in waves:
        remaining = max_steps - steps_used
        if remaining <= 0:
            return _budget_result()
        run_specs = wave if len(wave) <= remaining else wave[:remaining]
        # LG-3-4：本作用域里已完成的步骤直接复用落账结果，不再执行（副作用只发生一次）。
        skipped: Dict[str, TaskOutcome] = {}
        pending_specs: List[WaveTask] = []
        for spec in run_specs:
            record = done_steps.get(step_keys.get(spec.name, ""))
            if record is None:
                pending_specs.append(spec)
                continue
            skipped[spec.name] = TaskOutcome(
                name=spec.name,
                ok=True,
                value=record.get("value"),
                attempts=int(record.get("attempts") or 1),
            )
        if skipped:
            logger.info(
                "step ledger: skipped {} completed step(s) in wave {}",
                len(skipped),
                executed_waves,
            )
        batch: BatchOutcome = await run_batch(
            pending_specs,
            _unit,
            max_concurrency=int(settings.AGENT_MAX_CONCURRENCY or 0) or None,
            max_attempts=DEFAULT_MAX_ATTEMPTS,
        )
        executed = {outcome.name: outcome for outcome in batch.outcomes}
        merged: List[TaskOutcome] = [
            executed[spec.name] if spec.name in executed else skipped[spec.name]
            for spec in run_specs
        ]
        # F1e·A-P1-4①：进环挂起/恢复。必须在 run_batch 之外、本节点
        # （run_task_execution）之内调 interrupt——GraphInterrupt 向上传到
        # 图运行时挂起并写 checkpointer（thread_id），run_task 会吞掉它。
        # 恢复后本步重进本函数：interrupt() 立即返回裁决值——approve ⇒
        # 落批准（执行点消费）并把该步 outcome 换成成功再落账；reject/
        # 未知值/批准失败/重试仍失败 ⇒ fail-closed 快速失败：终止后续波次、
        # 本波照常落账（被拦步留失败观测）后直接回 failed 终态（保持本函数
        # 「只返回 LoopResult 不抛」的契约）。已知边界（LangGraph 节点重入）：
        # 挂起点之前的步骤在恢复后会重执行——`RECOVERY_STEP_LEDGER_ENABLED`
        # 开时由本函数上方的作用域清单跳过（LG-3-4）；关时非幂等写步骤仍须
        # 调用方自行幂等化。
        abort_reason: Optional[str] = None
        if pending is not None:
            payload = pending
            pending = None
            blocked_spec: WaveTask = payload.pop("spec")
            decision = review_interrupt(payload)  # type: ignore[misc]
            from agentix.tools.gateway.human_review import get_review_gate

            gate = get_review_gate()
            if decision == "approve" and payload.get("request_id"):
                if gate.approve(
                    str(payload["request_id"]),
                    by="in_loop_resume",
                    note="HITL in-loop resume",
                ):
                    retried = await executor.execute(
                        str(payload["tool"]), dict(payload["args"])
                    )
                    if retried.success:
                        for idx, outcome in enumerate(merged):
                            if outcome.name == blocked_spec.name:
                                merged[idx] = TaskOutcome(
                                    name=blocked_spec.name,
                                    ok=True,
                                    value=retried.data,
                                    attempts=outcome.attempts + 1,
                                    duration_ms=outcome.duration_ms,
                                )
                                break
                    else:
                        abort_reason = (
                            f"in_loop_review_failed: {payload['tool']}: "
                            f"{retried.error or 'retry blocked'}"
                        )
                else:
                    abort_reason = (
                        f"in_loop_review_failed: {payload['tool']}: approve not recorded"
                    )
            elif decision == "reject" and payload.get("request_id"):
                gate.reject(
                    str(payload["request_id"]),
                    by="in_loop_resume",
                    note="in-loop reject",
                )
                abort_reason = (
                    f"in_loop_review_rejected: {payload['tool']}: "
                    f"{payload.get('error') or 'rejected'}"
                )
            else:
                abort_reason = (
                    f"in_loop_review_failed: {payload['tool']}: "
                    f"bad resume decision {decision!r}"
                )
        if ledger is not None and pending_specs:
            outcomes_by_name = {outcome.name: outcome for outcome in merged}
            entries = [
                {
                    "step_key": step_keys[spec.name],
                    "tool": str(spec.payload.get("tool") or ""),
                    "value": outcomes_by_name[spec.name].value,
                    "attempts": outcomes_by_name[spec.name].attempts,
                }
                for spec in pending_specs
                if outcomes_by_name[spec.name].ok
            ]
            if entries:
                try:
                    await asyncio.to_thread(
                        ledger.mark_many, session_id, step_scope, entries
                    )
                except Exception as exc:  # noqa: BLE001 - 辅助子系统故障只降级（纪律 2）
                    logger.warning(
                        "step ledger mark failed; a later resume may replay these steps: {}",
                        exc,
                    )
        for spec, outcome in zip(run_specs, merged):
            steps_used += 1
            observations.append(_to_observation(spec, outcome, wave=executed_waves))
        if abort_reason is not None:
            logger.warning("in-loop HITL abort: {}", abort_reason)
            return _make_result(
                task_id=task.task_id,
                final=_synthesize(observations),
                passed=False,
                plan=plan,
                observations=observations,
                reason=abort_reason,
                terminal="failed",
                steps_used=steps_used,
            )
        total_attempts += batch.total_attempts
        executed_waves += 1
        if len(run_specs) < len(wave):
            logger.warning(
                "task budget hit mid-wave: {}/{} steps this wave",
                len(run_specs),
                len(wave),
            )
            return _budget_result()

    logger.info(
        "task waves done: {} waves, {} steps, {} attempts",
        executed_waves,
        steps_used,
        total_attempts,
    )

    # ---- 4. 分层校验 → 终态 ----------------------------------------------
    try:
        verification = await LayeredVerifier(
            require_grounding=bool(
                getattr(settings, "VERIFIER_GROUNDING_ENABLED", False)
            ),
            route=route,
        ).verify(task, plan, observations)
    except Exception as exc:  # noqa: BLE001 - 校验器异常 = failed（对齐 AgentLoop）
        reason = f"verify_failed: {type(exc).__name__}: {exc}"
        logger.warning("task verify raised: {}", reason)
        return _make_result(
            task_id=task.task_id,
            final=_synthesize(observations),
            passed=False,
            plan=plan,
            observations=observations,
            reason=reason,
            terminal="failed",
            steps_used=steps_used,
        )

    passed = bool(verification.passed)
    return _make_result(
        task_id=task.task_id,
        final=_synthesize(observations),
        passed=passed,
        plan=plan,
        observations=observations,
        reason=verification.reason or ("layered_ok" if passed else "verifier_rejected"),
        terminal="completed" if passed else "verify_failed",
        steps_used=steps_used,
        verification=verification,
    )
