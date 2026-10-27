"""会话与消息的编排层（P1-6：业务逻辑从 API 层下沉）。

设计要点（方案 P1-6）：
- 依赖注入 repository：``SessionRepository`` / ``MessageRepository`` 默认包装
  现有 CRUD 单例；单测可注入内存假实现，**不启动 FastAPI 即可测试**（验收 2）
- graph 通过 ``graph_getter`` 惰性获取（默认动态读 ``lg_builder.graph``），
  规避 P0-1 启动期覆盖的值绑定陷阱
- P0-3 错误分类链随编排走本层（AgentError→503/4xx、DEGRADABLE→降级 dict、
  LEGACY_ERROR_MODE 回滚、is_retryable 启发式、兜底 500）——这是业务语义
  （测试数据场景规范：降级兜底不能被当成测试数据答复），FastAPI 端点透传 HTTPException
- P1-6 兼容锚点：``chat.process_agent_query`` 保留为 ``ChatService.ask`` 的
  模块级别名，P0-3 测试（test_p03_p04_errors_observability.py）零改动
"""

import asyncio
import uuid
from datetime import datetime
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from fastapi import HTTPException
from loguru import logger
from sqlalchemy.orm import Session

from agentix.config.settings import DISPLAY_TZ

from agentix.orchestration.langgraph import lg_builder
from agentix.orchestration.langgraph.graph_config import thread_config
from agentix.application.services.budget_notice import record_budget_notice
from agentix.application.services.request_scope import (
    RequestScope,
    get_current_scope,
    record_tool,
    request_scope,
)
from agentix.config import ablation, settings
from agentix.domain.errors import AgentError, ErrorCode, HTTP_FATAL
from agentix.domain.policies import is_retryable
from agentix.domain.status import RequestStatus
from agentix.domain.tracing import get_trace_id
# P2-6 (T6)：Repository 默认实现下沉至 infrastructure，本层仅注入点保留。
from agentix.infrastructure.persistence.repositories import (
    MessageRepository,
    SessionRepository,
)
# P2-6 (T4)：KNOWN-ISSUE(P2-6) 已收口——application 不再 import interfaces 的
# HTTP 契约 schema，改用 domain.ports 的 stdlib TypedDict 载体（见
# agentix/domain/ports/payloads.py）。守卫（tests/test_p22_obs_scope.py）
# 棘轮白名单已收紧为空集：application → interfaces 的任何新导入都会让守卫失败。
from agentix.domain.ports import (
    MessageCreatePayload,
    MessageRepositoryPort,
    SessionCreatePayload,
    SessionRepositoryPort,
)

# P0-2(c)：agent 图执行的进程内并发上限（防 LLM 上游雪崩）。
# Python 3.10 的 asyncio 原语在构造时不绑定事件循环，模块级创建安全。
_AGENT_SEM = asyncio.Semaphore(settings.AGENT_MAX_CONCURRENCY)


# ---------------------------------------------------------------------------
# P2-2：请求级指标聚合辅助
# ---------------------------------------------------------------------------


def _scope_metrics_fields(scope: Optional[RequestScope]) -> Dict[str, Any]:
    """把当前请求作用域的聚合值映射为 ``RequestMetrics`` 字段。

    无活动作用域（scope is None）时返回空 dict——调用点用 ``**`` 展开后
    仍是合法的 RequestMetrics 默认值，绝不报错。
    """
    if scope is None:
        return {}
    return {
        "llm_calls": scope.llm_calls,
        "prompt_tokens": scope.prompt_tokens,
        "completion_tokens": scope.completion_tokens,
        "total_tokens": scope.total_tokens,
        "tools_used": list(scope.tools),
        "estimated_cost": scope.estimated_cost(
            input_price_per_1k=settings.LLM_INPUT_PRICE_PER_1K,
            output_price_per_1k=settings.LLM_OUTPUT_PRICE_PER_1K,
        ),
    }


def _extract_tool_names(messages: Any) -> List[str]:
    """从图结果的消息列表里 best-effort 提取工具名（仅非流式路径用）。

    非流式 ``ainvoke`` 不产生 ``on_tool_start`` 事件，只能事后从
    ``AIMessage.tool_calls`` 读取真实发生过的工具调用；提不到就返回空列表，
    绝不臆造数据。
    """
    names: List[str] = []
    for msg in messages or []:
        for call in getattr(msg, "tool_calls", None) or []:
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
            if name and name not in names:
                names.append(str(name))
    return names


def _record_tool_step(tool_name: str, phase: str, data: Any) -> None:
    """F6：把工具调用记为一条步骤级轨迹（start / end）。

    payload（工具入参）仅在 ``OBSERVABILITY_PAYLOAD_CAPTURE=True`` 时捕获，
    且经脱敏 + 截断。任何异常都被吞掉——轨迹记录绝不影响主链路。
    """
    try:
        from agentix.application.services.trajectory import (
            get_trajectory_recorder,
            payload_capture_enabled,
            redact_and_truncate,
        )

        detail: Dict[str, Any] = {}
        if payload_capture_enabled() and isinstance(data, dict):
            payload = data.get("input") if phase == "start" else data.get("output")
            if payload is not None:
                detail["payload_preview"] = redact_and_truncate(payload)
        get_trajectory_recorder().record(
            kind="tool",
            name=tool_name,
            status=phase,  # "start" | "end"
            detail=detail,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("tool trajectory record skipped: {}", exc)


def _record_error_step(error_code: str, message: str) -> None:
    """F6：把请求级失败记为一条 ``error`` 轨迹步骤（供首个错误步定位）。"""
    try:
        from agentix.application.services.trajectory import get_trajectory_recorder

        get_trajectory_recorder().record(
            kind="error",
            name=error_code,
            status="error",
            detail={"message": message},
            error=f"{error_code}: {message}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("error trajectory record skipped: {}", exc)


def _record_online_signal(
    *,
    success: bool,
    first_error_stage: str = "",
    cost_usd: float = 0.0,
    tags=(),
) -> None:
    """Phase 17.1：在线循环只记信号（零写）；任何异常吞掉。"""
    try:
        from agentix.agent.evolution import get_online_loop_recorder

        get_online_loop_recorder().record(
            trace_id=get_trace_id(),
            success=success,
            first_error_stage=first_error_stage,
            cost_usd=cost_usd,
            tags=tuple(tags),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("online loop record skipped: {}", exc)


# ---------------------------------------------------------------------------
# D 组：控制平面（spawn/status/cancel）与 token 预算
# ---------------------------------------------------------------------------


async def _invoke_tracked(coro: Any, *, agent_name: str = "kg_graph") -> Any:
    """在**控制平面**登记下执行一次图调用（spawn → running → 终态）。

    登记后 ``run_id`` 可被 ``ControlPlane.cancel()`` 打断——这是「cancel」
    从纸面变为可用的关键：只有把真实 ``asyncio.Task`` 绑定进运行实例，
    取消才能真正中断执行，而不是只改一个状态字段。

    控制平面是**可选增强**：登记失败（注册表故障）不影响调用（降级为直接
    await）；但**准入拒绝（AdmissionDenied）fail-closed 上抛**——超限就是
    不放行，登记故障 ≠ 策略拒绝（Phase 25）。
    """
    from agentix.runtime.control_plane import (
    AdmissionDenied,
    RunState,
    get_control_plane,
)
    try:
        # Phase H · W3：准入三态（flag 关首行直通）。拒绝抛 AdmissionDenied
        # → 落入下方既有 except（关协程 + fail-closed 上抛）。
        from agentix.performance.admission import admission_wait

        await admission_wait(kind="chat")
        from agentix.domain.tracing import get_trace_id

        plane = get_control_plane()
        run = plane.spawn(agent_name, trace_id=get_trace_id())
    except AdmissionDenied as exc:
        # 准入拒绝是**策略**不是故障：fail-closed 上抛（否则准入形同虚设）；
        # 关闭未 await 的协程防泄漏。
        close = getattr(coro, "close", None)
        if close is not None:
            close()
        logger.warning("control plane admission denied: {}", exc)
        raise
    except Exception as exc:  # noqa: BLE001 - 登记失败不得阻断主链路
        logger.debug("control plane spawn skipped: {}", exc)
        return await coro

    task = asyncio.ensure_future(coro)
    plane.bind(run.run_id, task)
    plane.mark_running(run.run_id)
    try:
        result = await task
    except asyncio.CancelledError:
        plane.finish(run.run_id, state=RunState.CANCELLED, error="cancelled")
        raise
    except BaseException as exc:  # noqa: BLE001 - 记录后原样抛出，不吞异常
        plane.finish(run.run_id, state=RunState.FAILED, error=f"{type(exc).__name__}: {exc}")
        raise
    else:
        plane.finish(run.run_id, state=RunState.SUCCEEDED)
        return result


def _build_budget_ledger() -> Any:
    """按 settings 构建请求级预算台账；未配置限额时返回 ``None``（不介入）。"""
    try:
        from agentix.application.services.token_budget import BudgetLedger

        per_request = int(getattr(settings, "TOKEN_BUDGET_PER_REQUEST", 0) or 0)
        per_agent = int(getattr(settings, "TOKEN_BUDGET_PER_AGENT", 0) or 0)
        if per_request <= 0:
            return None  # 0 = 不限额（默认，行为与接线前一致）
        return BudgetLedger.from_settings(
            per_request=per_request,
            per_agent=per_agent,
            max_tool_calls=int(getattr(settings, "TOOL_CALLS_PER_REQUEST", 0) or 0),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("token budget ledger skipped: {}", exc)
        return None


def _record_budget_tool_call() -> None:
    """把工具调用计入请求预算；预算设施异常不得阻断观测链路。"""
    try:
        from agentix.application.services.token_budget import get_current_ledger

        ledger = get_current_ledger()
        if ledger is not None and not ledger.record_tool_call():
            logger.warning(
                "Tool call budget exhausted: {}/{}",
                ledger.tool_calls,
                ledger.max_tool_calls,
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("tool call budget record skipped: {}", exc)


def _settle_token_budget(ledger: Any, scope: Optional[RequestScope]) -> Optional[Dict[str, Any]]:
    """把请求实际消耗记入预算并判定；返回台账快照（未启用时返回 ``None``）。

    判定为 ``WARN`` / ``EXCEEDED`` 时分别 info / error 日志——
    这是「预算生效」的**可观测证据**；硬中断由 LLM 层钩子承担（后续接线）。
    """
    if ledger is None or scope is None:
        return None
    try:
        from agentix.application.services.token_budget import BudgetVerdict

        ledger.consume(int(scope.total_tokens))
        verdict = ledger.verdict()
        snapshot = ledger.to_dict()
        if verdict is BudgetVerdict.EXCEEDED:
            logger.error("Token budget exceeded: {}", snapshot["request"])
        elif verdict is BudgetVerdict.WARN:
            logger.info("Token budget warning: {}", snapshot["request"])
        return snapshot
    except Exception as exc:  # noqa: BLE001
        logger.debug("token budget settle skipped: {}", exc)
        return None


def _completion_strict() -> bool:
    """终态校验是否**强制**（默认 False：只记录，不改判定）。"""
    return bool(getattr(settings, "COMPLETION_VALIDATOR_STRICT", False))


def _validate_completion_state(
    *,
    route: Optional[str],
    answer: Optional[str],
    sources: Any,
    status: Optional[str],
) -> Optional[Dict[str, Any]]:
    """A 组：依据环境终态事实校验完成度（确定性，零 LLM 调用）。

    返回 verdict 字典；任何异常都降级为 ``None``（**校验器不得成为新的单点故障**，
    与 `final_answer_guard` 的降级放行原则一致）。
    """
    try:
        from agentix.domain.policies.completion import validate_completion

        verdict = validate_completion(
            route=route,
            answer=answer,
            sources=sources,
            status=status,
        )
        return verdict.to_dict()
    except Exception as exc:  # noqa: BLE001
        logger.debug("completion validation skipped: {}", exc)
        return None


def _record_completion_step(completion: Dict[str, Any]) -> None:
    """把终态校验结论记为一条轨迹步骤（``node`` 类，便于与首个错误步对照）。"""
    try:
        from agentix.application.services.trajectory import get_trajectory_recorder

        signal = str(completion.get("signal", "unknown"))
        get_trajectory_recorder().record(
            kind="node",
            name="completion_validator",
            status="ok" if signal == "complete" else "error" if signal == "incomplete" else "skipped",
            detail={"signal": signal, "reasons": completion.get("reasons", [])},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("completion trajectory record skipped: {}", exc)


#: F1b2·M-P1-3：记忆索引常驻预算（字符）——索引本身也是上下文，封顶后
#: 只截**索引尾部**（低优先级的后置段）并附提示；常量决策，零新 settings 键
#: （与指令层 TOTAL_INSTRUCTION_CHARS 同一范式）。
MEMORY_INDEX_MAX_CHARS = 1500

#: 索引截断提示（与指令层截断提示同风格）
_MEMORY_INDEX_TRUNC_MARKER = "\n…[记忆索引已截断，可按需取回]"

#: 索引行的正文预览长度（索引只给「够不够相关」的判断依据，不倾倒全文）
_MEMORY_INDEX_PREVIEW_CHARS = 100

#: 上下文工程 B2：指令块进程级缓存 ``{"fingerprint": 键, "text": 文本}``。
#: 键由 :func:`_instructions_fingerprint` 计算（flag + 约定文件 stat + 触达
#: 目录）——文件不变即复用，避免每请求读盘；任何一层变化即整体重读。
_INSTRUCTIONS_CACHE: Dict[str, str] = {"fingerprint": "", "text": ""}


def _index_memory_context(ctx: str) -> str:
    """M-P1-3 MEMORY 索引制：记忆上下文 → 「索引 + obs:// 指针」（flag-on 专用）。

    - 非缩进且以 ``：`` 结尾的**段落头**（如「相关历史对话摘要：」）原样保留
      ——结构行给索引提供分组语境，本身不是正文；
    - 其余行 = 一个 topic：归一空白后经 ``offload_content`` 强制外置到
      ArtifactStore（kind=``memory_topic``，rebuildable——正文在 Redis LTM /
      卡片库可重建），入口替换为 ``- {预览} → obs://{artifact_id}`` 索引行；
      取回走 ``read_artifact``（obs:// 前缀自动剥离，与渐进上下文隔离引用
      共用同一条通道）；
    - **fail-open**：外置降级（offload_content 任何异常都返回内联）⇒ 该行
      保留原文，绝不丢记忆；
    - **常驻预算**：索引合计超 ``MEMORY_INDEX_MAX_CHARS`` ⇒ 截尾部 + 提示。
    """
    from agentix.application.services.artifact_store import offload_content

    lines: List[str] = []
    for raw in ctx.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if not line[0].isspace() and line.endswith("："):
            lines.append(line)  # 段落头：结构行原样保留
            continue
        topic = " ".join(line.split())
        result = offload_content(
            topic,
            kind="memory_topic",
            inline_threshold=1,  # 索引语义 = 强制外置（进本函数必转指针）
            rebuildable=True,
            meta={"source": "memory_context"},
        )
        if result.is_offloaded and result.ref is not None:
            preview = topic[:_MEMORY_INDEX_PREVIEW_CHARS] + (
                "…" if len(topic) > _MEMORY_INDEX_PREVIEW_CHARS else ""
            )
            lines.append(f"- {preview} → obs://{result.ref.artifact_id}")
        else:
            lines.append(topic)  # 降级：保留原文（fail-open 不丢内容）
    text = "\n".join(lines)
    if MEMORY_INDEX_MAX_CHARS > 0 and len(text) > MEMORY_INDEX_MAX_CHARS:
        keep = max(0, MEMORY_INDEX_MAX_CHARS - len(_MEMORY_INDEX_TRUNC_MARKER))
        text = text[:keep] + _MEMORY_INDEX_TRUNC_MARKER
    return text


async def _maybe_memory_context(message: str) -> str:
    """批次 7：按设置读回长期记忆上下文（默认关；失败绝不影响主链路）。

    - ``MEMORY_CONTEXT_ENABLED=False`` 时直接返回空串（零开销、零行为变化）；
    - 消融开关 ``AGENTIX_ABLATION_MEMORY_ENABLED=true`` 可绕过该 settings 打开召回，
      供变体 F_memory / G_full 使用（两者都要求 memory 打开，而生产默认是关的）；
    - Redis 不可用 / 记忆服务异常 → 记 warning 并返回空串（检索失败≠问答失败）；
    - 返回空串时调用方不会注入任何内容，与旧行为逐位一致；
    - P6-6.1（MemoryPort 扇出）：``MEMORY_FANOUT_ENABLED``（默认关，且挂在
      ``MEMORY_CONTEXT_ENABLED`` 通道之下）开时，在既有上下文后追加一段
      多 provider 召回的「长期记忆片段」——扇出失败只丢该段（base 保留），
      base 失败仍返回空串；flag-off 路径与基线逐位等价；
    - F1b2·M-P1-3：``MEMORY_INDEX_ENABLED``（默认关，同挂在
      ``MEMORY_CONTEXT_ENABLED`` 通道之下）开时，最终上下文再经
      :func:`_index_memory_context` 转为「索引 + obs:// 指针」；索引失败回
      原文（fail-open 不丢记忆），flag-off 与基线逐位等价。
    """
    if not (settings.MEMORY_CONTEXT_ENABLED or ablation.is_enabled("memory_enabled")):
        return ""
    try:
        from agentix.application.services.long_term_memory import get_long_term_memory

        base = await get_long_term_memory().get_context_for_query(
            message, max_summaries=settings.MEMORY_RECALL_LIMIT
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory recall failed (non-fatal): {}", exc)
        return ""
    if not getattr(settings, "MEMORY_FANOUT_ENABLED", False):
        ctx = base  # flag-off：与基线逐位等价
    else:
        try:
            extra = await _memory_fanout_context(message)
        except Exception as exc:  # noqa: BLE001 - 扇出失败不丢既有上下文
            logger.warning("memory fan-out recall failed (non-fatal): {}", exc)
            ctx = base
        else:
            ctx = f"{base}\n{extra}" if (base and extra) else (extra or base)
    if ctx and bool(getattr(settings, "MEMORY_INDEX_ENABLED", False)):
        try:
            return _index_memory_context(ctx)
        except Exception as exc:  # noqa: BLE001 - 索引失败回原文，绝不丢记忆
            logger.warning("memory index offload failed (non-fatal): {}", exc)
            return ctx
    return ctx


async def _memory_fanout_context(message: str) -> str:
    """P6-6.1：多 provider 扇出召回 → 格式化段落（空结果返回空串）。"""
    from agentix.application.services.memory_manager import get_memory_manager
    from agentix.domain.agent.types import MemoryScope

    items = await get_memory_manager().recall(
        scope=MemoryScope.USER.value,
        subject="default_user",  # 与 base 路径同语义（get_context 不传 user_id）
        query=message,
        limit=settings.MEMORY_RECALL_LIMIT,
    )
    if not items:
        return ""
    lines = ["长期记忆片段（扇出召回）："]
    for i, item in enumerate(items, 1):
        tags = f" [{'，'.join(item.tags)}]" if item.tags else ""
        lines.append(f"  {i}. {item.content}{tags}")
    return "\n".join(lines)


def _instructions_fingerprint(message: str) -> str:
    """指令块缓存键：相关 flag + 约定文件 stat + 触达目录 + skills 清单指纹。

    任一变化 ⇒ 键变 ⇒ 重读盘；全部不变 ⇒ 直接复用缓存文本（B2 指纹缓存，
    照抄 ``skills/registry.py`` 的 stat-热重载范式）。**任何异常都回退成
    「不可复用」的随机键**（fail-open：宁可多读一次盘，不可复用陈旧指令）。
    """
    from pathlib import Path

    from agentix.agent.context.instructions import (
        GLOBAL_INSTRUCTION_SUBDIR,
        PROJECT_INSTRUCTION_FILES,
        find_project_instructions,
        mentioned_instruction_dirs,
    )

    skills_on = bool(getattr(settings, "SKILLS_ENABLED", False))
    cascade_on = bool(getattr(settings, "AGENTS_CASCADE_ENABLED", False))
    parts: List[str] = [f"skills={int(skills_on)}", f"cascade={int(cascade_on)}"]
    project = find_project_instructions()
    if project is None:
        parts.append("project=-")
    else:
        try:
            st = project.stat()
            parts.append(f"project={project}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append(f"project={project}")
    if cascade_on:
        for name in PROJECT_INSTRUCTION_FILES:
            candidate = Path.home() / GLOBAL_INSTRUCTION_SUBDIR / name
            if candidate.is_file():
                try:
                    st = candidate.stat()
                    parts.append(f"global={candidate}:{st.st_mtime_ns}:{st.st_size}")
                except OSError:
                    parts.append(f"global={candidate}")
                break
    parts.extend(f"sub={d}" for d in mentioned_instruction_dirs(message))
    if skills_on:
        try:
            from agentix.agent.skills.loader import scan_fingerprint

            parts.append(f"skills_fp={scan_fingerprint(settings.SKILLS_DIR)!r}")
        except Exception:  # noqa: BLE001 - 指纹算不出就禁用缓存
            return f"uncacheable-{uuid.uuid4().hex}"
    return "|".join(parts)


async def _maybe_instructions(message: str) -> str:
    """上下文工程 B：L2 指令分层块（``LG_INSTRUCTIONS_ENABLED``，默认关）。

    - flag-off 直接返回空串（零 IO、config 与基线逐位等价）；
    - flag-on 走 :func:`build_instructions`（settings → skills → 全局 →
      项目 → 子目录 → 会话，4k/12k 预算 + hash 去重），结果经指纹缓存——
      文件不变不重读盘；读盘经 ``asyncio.to_thread`` 不阻塞事件循环；
    - **fail-open**：任何异常返回空串（约定文件读不到 ≠ 问答失败），
      注入方对空串不注入任何内容。
    """
    if not bool(getattr(settings, "LG_INSTRUCTIONS_ENABLED", False)):
        return ""
    try:
        from agentix.agent.context.instructions import (
            build_instructions,
            mentioned_instruction_dirs,
        )

        fingerprint = _instructions_fingerprint(message)
        cached = _INSTRUCTIONS_CACHE
        if cached.get("fingerprint") == fingerprint:
            return str(cached.get("text") or "")
        subdirs = mentioned_instruction_dirs(message)
        text = await asyncio.to_thread(
            lambda: "".join(
                block + "\n\n" for block in build_instructions(subdirs=subdirs)
            )
        )
    except Exception as exc:  # noqa: BLE001 - 指令块故障不得影响主链路
        logger.warning("instruction cascade failed (non-fatal): {}", exc)
        return ""
    _INSTRUCTIONS_CACHE["fingerprint"] = fingerprint
    _INSTRUCTIONS_CACHE["text"] = text
    return text


def _inject_tool_policy(config: Dict[str, Any]) -> Dict[str, Any]:
    """P2（双层闸 L1）：``TOOL_SELECTION_ENABLED`` 时注入 ``tool_policy``。

    tool_selection 节点读取 ``configurable["tool_policy"]`` 的逻辑一直在
    （读取点），但全仓没有注入方 ⇒ 生产从不过滤（死读）。本函数补齐注入方：

    - flag-off（默认）**完全不触碰 config**——与基线逐位等价；
    - flag-on 注入 ``ToolSelectionPolicy(max_risk_level=TOOL_SELECTION_MAX_RISK)``
      （非法值由 ``build_tool_policy`` 记 error 并返回 None ⇒ 同样不注入）；
    - 注入的是**声明级可见面**，执行层 ``ToolExecutor`` 的参数感知有效级
      判定独立生效（见 selection.py 分工说明），两层缺一即漏。
    """
    if not bool(getattr(settings, "TOOL_SELECTION_ENABLED", False)):
        return config
    from agentix.tools.selection import build_tool_policy

    policy = build_tool_policy()
    if policy is not None:
        config.setdefault("configurable", {})["tool_policy"] = policy
    return config


# ---------------------------------------------------------------------------
# Repository 默认实现（P2-6 T6 已下沉至 infrastructure/persistence/repositories.py）
#
# 本层只保留 **依赖抽象**：注入点类型是 domain.ports 的 Protocol；
# 默认实现（SessionRepository / MessageRepository）从 infrastructure 导入，
# 测试可注入内存假实现替换。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ChatService
# ---------------------------------------------------------------------------

#: A1d：metadata 死重键——**不落库、不外泄**的观测/调试负载（全仓无读者：
#: ``agent_state`` 整图状态数 KB；``runtime`` 类型化轨迹；``budget`` 台账
#: 快照；``completion`` 完成度校验中间态——三者均已在写入点上游消费完，
#: 只剩响应体/DB 行里的冗余）。``save_message``（落库）与接口层
#: ``_public_metadata``（HTTP 响应）共用本集合，口径单点同步。
METADATA_EXCLUDE_KEYS = frozenset({"agent_state", "runtime", "budget", "completion"})


class ChatService:
    """会话 / 消息 / Agent 编排层。依赖注入 repository，不直接触碰 ORM。"""

    def __init__(
        self,
        # P1-7：注入点类型收敛为 domain.ports 的 Protocol（依赖抽象，非具体实现）
        session_repo: Optional[SessionRepositoryPort] = None,
        message_repo: Optional[MessageRepositoryPort] = None,
        graph_getter: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._session_repo = session_repo or SessionRepository()
        self._message_repo = message_repo or MessageRepository()
        # 惰性 graph 获取：默认动态读 lg_builder.get_active_graph()
        # （= 按 multi_agent 消融开关返回扁平图或 supervisor 图；flag-off 恒扁平）。
        # 启动期 Redis checkpointer 图覆盖 lg_builder.graph 后仍动态取到当前值。
        self._graph_getter = graph_getter or (lambda: lg_builder.get_active_graph())

    # ---------------- 会话编排 ----------------

    def get_or_create_session(
        self, db: Session, session_id: Optional[str], user_id: str
    ) -> str:
        """Get existing session or create new one"""
        if session_id:
            session = self._session_repo.get(db, id=session_id)
            if session:
                return session_id

        # Create new session
        new_session_id = str(uuid.uuid4())
        session_data = SessionCreatePayload(
            id=new_session_id,
            user_id=user_id,
            title=f"Chat {datetime.now(DISPLAY_TZ).strftime('%Y-%m-%d %H:%M')}",
        )
        self._session_repo.create(db, obj_in=session_data)
        return new_session_id

    def session_exists(self, db: Session, session_id: str) -> bool:
        """会话存在性校验（history/clear 等端点 404 兜底，防资源探测）"""
        return self._session_repo.get(db, id=session_id) is not None

    def get_session_owner(self, db: Session, session_id: str) -> Optional[str]:
        """会话归属 user_id（审计批次 A1 归属校验）。

        会话不存在 → ``None``；存在但 user_id 为空的历史行 → ``None``。
        调用方须先 ``session_exists`` 区分「不存在」与「无主」两种 ``None``。
        """
        session = self._session_repo.get(db, id=session_id)
        if session is None:
            return None
        return getattr(session, "user_id", None)

    # ---------------- 消息编排 ----------------

    async def save_message(
        self,
        db: Session,
        session_id: str,
        message: str,
        is_user: bool,
        route: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> Optional[str]:
        """Save message to database"""
        try:
            last_message = self._message_repo.get_latest_by_session(
                db, session_id=session_id
            )
            next_order_index = (last_message.order_index + 1) if last_message else 1

            message_metadata: Dict[str, Any] = {}
            if isinstance(metadata, dict):
                # A1d：过滤死重键（agent_state/runtime/budget/completion——
                # 全仓无读者，落库只膨胀行体积；其余键原样保留）。
                message_metadata.update(
                    {
                        k: v
                        for k, v in metadata.items()
                        if k not in METADATA_EXCLUDE_KEYS
                    }
                )
            elif metadata is not None:
                message_metadata["metadata"] = str(metadata)

            if route:
                message_metadata["route"] = route

            message_data = MessageCreatePayload(
                session_id=session_id,
                message_type="user_query" if is_user else "agent_response",
                content=message,
                message_metadata=message_metadata or None,
                order_index=next_order_index,
            )
            created = self._message_repo.create(db, obj_in=message_data)

            # Update session activity timestamp so session list ordering stays correct.
            self._session_repo.update_activity(db, session_id=session_id)
            return str(created.id)
        except Exception as e:
            # P0 接口修复：回滚挂起事务，避免脏 session 影响同请求内的后续写操作
            try:
                db.rollback()
            except Exception:  # noqa: BLE001 - rollback 自身失败不影响主流程
                pass
            logger.error(f"Failed to save message: {e}")
            return None

    def list_messages(
        self, db: Session, *, session_id: str, skip: int = 0, limit: int = 100
    ) -> List[Any]:
        """按会话取消息（history 端点数据源）。"""
        return self._message_repo.get_by_session(
            db, session_id=session_id, skip=skip, limit=limit
        )

    def list_recent_messages(
        self, db: Session, *, session_id: str, skip: int = 0, limit: int = 100
    ) -> List[Any]:
        """最近 N 条历史（接口审查批次 2026-09-28：history 端点数据源）。

        skip/limit 从**最新**往回数，返回仍是正序时间线——会话超长时
        保留的是最新问答而非最旧问答。
        """
        return self._message_repo.get_recent_by_session(
            db, session_id=session_id, skip=skip, limit=limit
        )

    def clear_session_messages(self, db: Session, *, session_id: str) -> int:
        """清空会话消息（clear 端点数据源）。"""
        return self._message_repo.delete_by_session(db, session_id=session_id)

    # ---------------- Agent 编排 ----------------

    async def ask(
        self,
        message: str,
        session_id: str,
        image_path: Optional[str] = None,
        file_path: Optional[str] = None,
        ingest_incremental: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Process query through agent system（含 P0-3 错误分类链）。

        P2-2：整个请求处理包在 ``request_scope`` 内——进入时建立全新的累加器，
        退出时（含异常/提前 raise）**必定 reset**，不会把本次的 token/tool
        统计泄漏到下一次请求。具体逻辑见 ``_ask_impl``。
        """
        async with request_scope() as scope:
            # Phase H · W3：run 墙钟 deadline 挂到 scope（跨 gather 子任务
            # 共享）；ADAPTIVE_TOOL_TIMEOUT_ENABLED 关时为惰性数据零影响。
            scope.set_deadline_seconds(settings.AGENT_TIMEOUT)
            return await self._ask_impl(
                message, session_id, image_path, file_path, ingest_incremental
            )

    async def _ask_impl(
        self,
        message: str,
        session_id: str,
        image_path: Optional[str] = None,
        file_path: Optional[str] = None,
        ingest_incremental: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """``ask`` 的实际实现（在 ``request_scope`` 上下文内执行）。"""
        # Phase 22：配置变更监测——运行中改配置 ⇒ 轨迹 append system_notice
        # （仅键名/哈希无值无 PII），不重建静态前缀（KV Cache 纪律）。
        from agentix.application.services.config_watch import notice_if_changed

        notice_if_changed()

        incremental_flag = (
            settings.INGEST_INCREMENTAL_DEFAULT
            if ingest_incremental is None
            else bool(ingest_incremental)
        )
        # P0-2(a)：图级最大轮数硬边界——config 形状（顶层 recursion_limit）只从
        # graph_config 出（LG-2-3）。该键不可塞进 configurable，否则图不识别。
        config = thread_config(
            session_id,
            image_path=image_path,
            file_path=file_path,
            incremental=incremental_flag,
        )

        # F1e·A-P1-4：非 stream ask 声明进环意图（lg_builder 注入壳据此决定
        # 是否塞 interrupt 回调）。stream 恒 L2 不进本分支 ⇒ 永不声明、
        # 永不挂起；flag-off 不触碰 config（与基线逐位等价）。
        if settings.HITL_IN_LOOP_ENABLED:
            config["configurable"]["hitl_in_loop"] = 1

        input_state = {
            "messages": [{"type": "human", "content": message}]
        }

        # 批次 7：长期记忆读回（MEMORY_CONTEXT_ENABLED，默认关）。
        # 经 configurable 传递，不进 messages 通道——不会污染 checkpointer 历史。
        memory_context = await _maybe_memory_context(message)
        if memory_context:
            config["configurable"]["memory_context"] = memory_context

        # 上下文工程 B3：指令分层块（LG_INSTRUCTIONS_ENABLED，默认关）。
        # 与 memory_context 同一 transient 通道；general_handler 里按
        # 「静态 system 常量 → 指令块 → memory_context」顺序拼接。
        instructions = await _maybe_instructions(message)
        if instructions:
            config["configurable"]["instructions"] = instructions

        # P2：tool_policy 注入（TOOL_SELECTION_ENABLED，默认关 = 不触碰 config）。
        _inject_tool_policy(config)

        # P0-4(c)：请求级指标（延迟导入避免加重模块导入链）
        import time as _time

        from agentix.application.services.observability import (
            RequestMetrics,
            get_observability,
        )

        t0 = _time.perf_counter()
        # D 组：请求级 token 预算台账（TOKEN_BUDGET_PER_REQUEST=0 ⇒ None，不介入）
        budget_ledger = _build_budget_ledger()
        if budget_ledger is not None:
            # 绑定到当前请求作用域，使 LLM 调用层能「先判断再调用」——
            # 预算因此从「只计量」升级为「真限额」（默认 0 ⇒ 不启用，行为不变）。
            from agentix.application.services.token_budget import set_current_ledger

            set_current_ledger(budget_ledger)

        # 上下文工程 D：状态尾注的预算段（LG_STATUS_TAIL_ENABLED 默认关 =
        # 不触碰 config）。只有真有限额（ledger 存在）才有「余量」可言；
        # general_handler 把它拼到**本轮 human 消息尾部**（append-only，
        # 不进静态前缀 ⇒ 不打穿 KV Cache）。
        if budget_ledger is not None and bool(
            getattr(settings, "LG_STATUS_TAIL_ENABLED", False)
        ):
            config["configurable"]["budget_status"] = "; ".join(
                budget_ledger.snapshot_lines()
            )

        # R3-3（Agent Runtime 架构 §95/§100）：每请求挂 AgentRuntime。
        # P0 观测模式——Budget 六维全 0 = 不限额，Runtime 只产出类型化轨迹
        # （AgentState/ExecutionStep），不改变任何执行语义（Strangler Phase 1）。
        from agentix.runtime import (
            AgentRuntime,
            Budget as RuntimeBudget,
            StepType,
            Task as RuntimeTask,
        )

        runtime = AgentRuntime(
            session_id=session_id,
            budget=RuntimeBudget(
                max_steps=0,
                max_llm_calls=0,
                max_tool_calls=0,
                max_tokens=0,
                max_latency_ms=0,
                max_cost_usd=0.0,
            ),
        )
        runtime_state = runtime.begin(task=RuntimeTask(kind="chat", question=message))
        try:
            # Invoke agent graph（P0-2(c)：信号量限流，并发不超过 AGENT_MAX_CONCURRENCY）
            # graph_getter 惰性访问：启动期可能被 Redis checkpointer 图覆盖
            async with _AGENT_SEM:
                try:
                    # P0-2(a)：整图执行的墙钟硬边界。AGENT_TIMEOUT 由此真正生效，
                    # 防止单请求挂死长期占用并发额度（信号量槽位）。
                    # D 组：_invoke_tracked 在控制平面登记本次运行（可被 cancel 打断）。
                    # R3-3：整图执行包成 Runtime 的 EXECUTING 步骤（失败自动落账）。
                    # Phase 15：AGENT_LOOP_ENABLED=True ⇒ ask 走 AgentLoop（L3）；
                    # 默认 False ⇒ 原 L2 LangGraph 路径零变化。stream 恒 L2（决策 A）。
                    async with runtime.astep(
                        StepType.EXECUTING, "agent_graph", input_data=input_state
                    ):
                        if settings.AGENT_LOOP_ENABLED:
                            from agentix.application.services.agent_loop_adapter import (
                                run_agent_loop_ask,
                            )

                            result = await asyncio.wait_for(
                                _invoke_tracked(
                                    run_agent_loop_ask(
                                        message=message,
                                        session_id=session_id,
                                    ),
                                    agent_name="agent_loop",
                                ),
                                timeout=settings.AGENT_TIMEOUT,
                            )
                        else:
                            result = await asyncio.wait_for(
                                _invoke_tracked(
                                    self._graph_getter().ainvoke(
                                        input_state, config=config
                                    )
                                ),
                                timeout=settings.AGENT_TIMEOUT,
                            )
                except asyncio.TimeoutError as exc:
                    # 保留原始超时原因（raise ... from exc），供 DEBUG 下排查
                    runtime.abort(
                        "agent_timeout",
                        f"agent 执行超时（{settings.AGENT_TIMEOUT}s）",
                        stage="executing",
                    )
                    raise AgentError(
                        ErrorCode.AGENT_TIMEOUT,
                        f"agent 执行超时（{settings.AGENT_TIMEOUT}s）",
                    ) from exc

            # D 组：把实际消耗记入预算并判定（未启用预算时返回 None）
            budget_snapshot = _settle_token_budget(budget_ledger, get_current_scope())

            # Extract response and metadata
            response_text = ""
            if result.get("messages"):
                response_text = result["messages"][-1].content

            # Extract route information
            router_info = result.get("router", {})
            route = router_info.get("type")
            route_logic = router_info.get("logic")
            # R3-3：路由结论写回 Runtime 任务（轨迹完整性）
            runtime_state.task.route_type = route

            # Extract sources if available
            sources_raw = result.get("sources", [])

            # Convert sources to expected format (list of dicts)
            sources = []
            if sources_raw:
                # If sources is a list of strings, convert to list of dicts
                if isinstance(sources_raw[0], str):
                    for src in sources_raw:
                        sources.append({"document_id": src, "source": src})
                else:
                    # Already in correct format
                    sources = sources_raw

            # P0-3：下层节点（general_handler fallback 等）可能标记降级
            last_msg = (
                result.get("messages", [None])[-1] if result.get("messages") else None
            )
            degraded = bool(
                getattr(last_msg, "additional_kwargs", {}).get("degraded", False)
            ) if last_msg is not None else False

            # P2-2：非流式路径看不到 on_tool_start 事件，只能事后从结果消息里
            # best-effort 提取真实发生过的工具调用（提不到就保持空，不臆造）。
            for _tool_name in _extract_tool_names(result.get("messages")):
                record_tool(_tool_name)
                _record_budget_tool_call()
                # R3-3：工具调用同步记入 Runtime 预算台账（观测维度）
                runtime.record_tool_call()

            # R3-3：正常收尾——VERIFYING → COMPLETED，预算快照写回轨迹
            runtime.finish(output={"route": route, "degraded": degraded})

            # A 组：终态完成度校验（**确定性**，不调 LLM）。
            # 默认只记录不改判定（COMPLETION_VALIDATOR_STRICT=False）——
            # 先把「模型自认完成 vs 环境事实」的差异观测出来，再决定是否强制。
            completion = _validate_completion_state(
                route=route,
                answer=response_text,
                sources=sources,
                status=RequestStatus.DEGRADED.value if degraded else RequestStatus.OK.value,
            )
            if completion is not None:
                _record_completion_step(completion)
                if completion["signal"] == "incomplete" and _completion_strict():
                    degraded = True

            # P0-4(c) + P2-2：请求成功指标（含请求级 token/tool/cost 聚合；
            # 上报失败不影响主链路）
            try:
                await get_observability().record_request(
                    RequestMetrics(
                        request_id=get_trace_id(),
                        session_id=session_id,
                        route_type=route or "",
                        total_latency_ms=(_time.perf_counter() - t0) * 1000,
                        success=True,
                        **_scope_metrics_fields(get_current_scope()),
                    )
                )
            except Exception as obs_exc:  # noqa: BLE001
                logger.warning("record_request failed (non-fatal): {}", obs_exc)
            # Phase 17.1：在线循环只记信号（零写能力，三漏斗之一：成功）
            _record_online_signal(success=True, tags=("ask",))
            # F1b·M-P1-2：turn 结束后台评审（flag-off 零调度）
            self._maybe_schedule_turn_review(question=message, answer=response_text)

            return {
                "message": response_text,
                "route": route,
                "route_logic": route_logic,
                "sources": sources,
                "status": (
                    RequestStatus.DEGRADED.value if degraded else RequestStatus.OK.value
                ),
                "error_code": None,
                "metadata": {
                    "session_id": session_id,
                    "agent_state": result,
                    "budget": budget_snapshot,
                    "completion": completion,
                    # R3-3：类型化运行轨迹（AgentState 快照；纯新增键，不影响既有消费方）
                    "runtime": runtime_state.to_dict(),
                },
            }
        except HTTPException:
            raise
        except AgentError as e:
            # P0-3：Agent 链路显式错误——按四层分类各自兜底
            logger.warning(
                "agent error code={} msg={} detail={}", e.code, e.message, e.detail
            )
            _record_error_step(e.code.value, e.message)
            # R3-3：Runtime 轨迹同步落 FAILED（错误码/信息进轨迹）
            runtime.abort(e.code.value, e.message, stage="executing")
            http_status = HTTP_FATAL.get(e.code)
            if http_status is not None:
                raise HTTPException(
                    status_code=http_status,
                    detail={
                        "error_code": e.code.value,
                        "message": e.message,
                        **({"detail": e.detail} if settings.DEBUG else {}),
                    },
                )
            # 可降级错误：仍返回 200，但带结构化 status/error_code
            # （不上报 assistant 消息到会话历史，避免污染上下文）
            try:
                await get_observability().record_request(
                    RequestMetrics(
                        request_id=get_trace_id(),
                        session_id=session_id,
                        total_latency_ms=(_time.perf_counter() - t0) * 1000,
                        success=False,
                        error=e.code.value,
                        **_scope_metrics_fields(get_current_scope()),
                    )
                )
            except Exception as obs_exc:  # noqa: BLE001
                logger.warning("record_request failed (non-fatal): {}", obs_exc)
            # Phase 17.1：三漏斗之二 —— AgentError 可降级路径
            _record_online_signal(
                success=False,
                first_error_stage=e.code.value,
                tags=("ask", "agent_error"),
            )
            return {
                "message": "抱歉，相关测试数据检索服务暂时不可用，请稍后重试。",
                "route": "degraded",
                "route_logic": f"AgentError: {e.code.value}",
                "sources": [],
                "status": RequestStatus.DEGRADED.value,
                "error_code": e.code.value,
                "metadata": {
                    "session_id": session_id,
                    # R3-3：降级路径同样携带失败轨迹（供排障/评测）
                    "runtime": runtime_state.to_dict(),
                },
            }
        except Exception as e:
            # P0-4(c) + P2-2：请求失败指标（含请求级聚合）
            _record_error_step("internal_error", f"{type(e).__name__}: {e}")
            # R3-3：Runtime 轨迹同步落 FAILED
            runtime.abort("internal_error", f"{type(e).__name__}: {e}", stage="executing")
            try:
                await get_observability().record_request(
                    RequestMetrics(
                        request_id=get_trace_id(),
                        session_id=session_id,
                        total_latency_ms=(_time.perf_counter() - t0) * 1000,
                        success=False,
                        error=f"{type(e).__name__}: {e}",
                        **_scope_metrics_fields(get_current_scope()),
                    )
                )
            except Exception as obs_exc:  # noqa: BLE001
                logger.warning("record_request failed (non-fatal): {}", obs_exc)
            # Phase 17.1：三漏斗之三 —— 未预期内部错误
            _record_online_signal(
                success=False,
                first_error_stage="internal_error",
                tags=("ask", "internal_error"),
            )

            if settings.LEGACY_ERROR_MODE:
                # 回滚开关：恢复旧行为（吞异常 + 200 + 兜底文案）
                logger.error(f"Agent query failed: {e}", exc_info=True)
                return {
                    "message": "抱歉，处理您的请求时出现了错误。请稍后重试。",
                    "route": "error",
                    "route_logic": f"Error: {str(e)}",
                    "sources": [],
                    "status": RequestStatus.DEGRADED.value,
                    "error_code": "internal_error",
                    "metadata": {"error": str(e)},
                }
            # 启发式分类：瞬时类异常（连接/超时/429/5xx）→ 上游不可用（503 语义）
            if is_retryable(e):
                logger.error("agent query failed (upstream transient): {}", e)
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error_code": ErrorCode.LLM_UNAVAILABLE.value,
                        "message": "上游模型服务暂时不可用，请稍后重试。",
                        **({"detail": str(e)} if settings.DEBUG else {}),
                    },
                )
            # 未预期异常 → 500，DEBUG 下带内部细节
            logger.error(f"Agent query failed: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail={
                    "error_code": "internal_error",
                    "message": "服务内部错误，请稍后重试。",
                    **({"detail": str(e)} if settings.DEBUG else {}),
                },
            )
        finally:
            # 无论成功/失败都必须解绑预算台账——ContextVar 会跨请求复用同一个
            # 执行上下文（尤其线程池/长连接场景），不解绑会造成预算"串台"。
            if budget_ledger is not None:
                from agentix.application.services.token_budget import (
                    clear_current_ledger,
                )

                clear_current_ledger()

    # P1-4(b)：面向用户的「回答类」节点白名单——只转发这些节点的 LLM token。
    #
    # 主图/KB 子图内部还会跑路由、guardrails、planner、tool_selection、summarize
    # 等内部编排 LLM，若无条件转发，用户会在答案流里看到路由 JSON、guardrail
    # 判断、工具选择推理等内部碎片（体验缺陷）。tags 无法区分——KB 子图内
    # router/finalize/guardrails 共用同一 llm 实例（role_tag 恒为 general），
    # 因此改用事件 metadata 里的 ``langgraph_node`` 做节点级过滤。
    _STREAM_FORWARD_NODES = frozenset(
        {
            # 主图回答类节点
            "respond_to_general_query",
            "get_additional_info",
            "create_kb_query",
            "create_research_plan",
            "create_image_query",
            "create_file_query",
            # 子图/其他图里的回答类节点
            "final_answer",
            "finalize",
        }
    )

    @classmethod
    def _is_answer_node(cls, node_name: Any) -> bool:
        """判断 ``langgraph_node`` 是否属于「回答类」节点。

        实测（langgraph 0.2.60）：子图节点的事件里 ``langgraph_node`` 就是
        **裸节点名**（如 ``finalize``），不带命名空间前缀。这里仍按「取最后一段
        冒号分隔名」做防御性归一，以兼容潜在的 ``kb_workflow:finalize`` 形式。
        缺失/未知节点名一律不转发（由整段兜底保证不空）。
        """
        if not node_name:
            return False
        return str(node_name).split(":")[-1] in cls._STREAM_FORWARD_NODES

    @staticmethod
    def _extract_chunk_text(chunk: Any) -> str:
        """从消息对象/分片/字符串里提取纯文本。

        兼容：``str``、带 ``.content`` 的消息块（``AIMessageChunk``）、
        形如 ``{"content": ...}`` 的 dict，以及 ``content`` 为多模态 part
        列表的情况（仅拼接 ``text`` 部分）。
        """
        if chunk is None:
            return ""
        if isinstance(chunk, dict) and "content" in chunk:
            chunk = chunk["content"]
        content = getattr(chunk, "content", chunk)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            return "".join(parts)
        return ""

    @classmethod
    def _extract_final_text(cls, output: Any) -> str:
        """从图最终输出取 ``messages[-1].content``（整段兜底用）。"""
        if not isinstance(output, dict):
            return ""
        messages = output.get("messages")
        if not messages:
            return ""
        return cls._extract_chunk_text(messages[-1])

    @staticmethod
    def _extract_sources(output: Any) -> List[Any]:
        """从图最终输出提取 ``sources``（拿不到则返回空 list，不破坏流式）。"""
        if not isinstance(output, dict):
            return []
        raw = output.get("sources")
        if not raw:
            return []
        if isinstance(raw, (list, tuple)):
            return list(raw)
        return [raw]

    async def stream_events(
        self,
        message: str,
        session_id: str,
        image_path: Optional[str] = None,
        file_path: Optional[str] = None,
        ingest_incremental: Optional[bool] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """流式编排入口（P2-2 包一层 ``request_scope`` + 终态落库）。

        作用域覆盖整条事件流的消费期，使流式路径里的 LLM 用量
        （``llm_factory`` 的 ``record_llm_usage``）与工具调用
        （``on_tool_start`` 的 ``record_tool``）能被聚合；生成器结束/被提前
        ``aclose`` 时上下文必定 reset，不跨请求串味。

        P2-2 补充：流式（``/chat/stream``、``/stream``）是当前主用户路径，
        费用需按「总任务」口径核算，故在**首个终态帧**（``error`` / ``done``）
        处 ``record_request`` **恰好一次**；SSE 帧内容与顺序零改动。

        已知局限：客户端**中途断连**时本生成器被 ``aclose``，终态帧不会被
        产出，该请求可能不被记录——不用 ``try/finally`` 硬凑一个可能失真的
        成功记录。事件语义详见 ``_stream_events_impl``。
        """
        import time as _time

        from agentix.application.services.observability import (
            RequestMetrics,
            get_observability,
        )

        t0 = _time.perf_counter()
        recorded = False
        route_type = ""
        errored = False

        async def _finalize_stream_metrics(*, success: bool, error: Optional[str]) -> None:
            """终态落库一次（失败绝不影响事件流）。"""
            try:
                await get_observability().record_request(
                    RequestMetrics(
                        request_id=get_trace_id(),
                        session_id=session_id,
                        route_type=route_type,
                        total_latency_ms=(_time.perf_counter() - t0) * 1000,
                        success=success,
                        error=error,
                        **_scope_metrics_fields(get_current_scope()),
                    )
                )
            except Exception as obs_exc:  # noqa: BLE001
                logger.warning("record_request failed (non-fatal): {}", obs_exc)

        async with request_scope() as scope:
            # Phase H · W3：流式路径同挂 run deadline（工具层自适应超时取
            # 其剩余预算；flag 关时为惰性数据零影响）。
            scope.set_deadline_seconds(settings.AGENT_TIMEOUT)
            # A5/R5a：run-scoped emitter——impl 在首帧前把登记成功的 run_id
            # 换装进 holder；登记失败/降级保持无 run_id 的默认发射器（帧仍
            # 带 seq）。本层是 SSE 投影的**单点**戳记处（守卫 #5：每帧可
            # 回溯到统一 Run/Event stream）。
            from agentix.application.services.run_event_stream import (
                RunEventEmitter,
            )

            run_scope: Dict[str, Any] = {"emitter": RunEventEmitter()}
            async for event in self._stream_events_impl(
                message,
                session_id,
                image_path,
                file_path,
                ingest_incremental,
                run_scope=run_scope,
            ):
                event = run_scope["emitter"].attach(event)
                etype = event.get("type")
                if etype == "metadata":
                    route = (event.get("metadata") or {}).get("route")
                    if route:
                        route_type = route
                # 只在**首个终态帧**落库一次；在转发该帧之前 await，
                # 保证只要走到终态就一定被记录（客户端中途断连则不记录，见上）。
                if not recorded and etype in ("error", "done"):
                    if etype == "error":
                        errored = True
                    await _finalize_stream_metrics(
                        success=not errored,
                        error="stream_error" if errored else None,
                    )
                    recorded = True
                yield event

    def _maybe_schedule_turn_review(self, *, question: str, answer: str) -> None:
        """F1b·M-P1-2（memory-optimization §5 后台评审）：turn 结束异步评审。

        flag ``MEMORY_REVIEW_ENABLED``（默认关）⇒ 零调度零开销（基线逐位
        等价）；flag-on ⇒ fire-and-forget 把 turn 文本送进后台评审
        （``extract_cards`` → LLM 路由，内部 fail-open 回规则），不阻塞
        响应；调度失败只打点，绝不影响主链。
        """
        if not settings.MEMORY_REVIEW_ENABLED:
            return
        try:
            from agentix.application.services.memory_review import (
                schedule_review_for_summary,
            )

            schedule_review_for_summary(
                f"user: {question}\nassistant: {answer}", use_llm=True
            )
        except Exception as exc:  # noqa: BLE001 - 评审调度失败不影响主链
            logger.warning(
                "turn background review schedule failed (non-fatal): {}", exc
            )

    async def _stream_events_impl(
        self,
        message: str,
        session_id: str,
        image_path: Optional[str] = None,
        file_path: Optional[str] = None,
        ingest_incremental: Optional[bool] = None,
        run_scope: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """``stream_events`` 的实际实现（在 ``request_scope`` 上下文内迭代）。

        ``run_scope``（A5/R5a）：调用方可传入 holder dict；登记成功后
        以 Run作用域 :class:`RunEventEmitter` 替换 ``run_scope["emitter"]``
        供外层给每帧打 ``seq``/``run_id``。直接调用本实现（测试等）不传
        即可，行为与 A1 一致。

        P1-4：从「伪流式」升级为 LangGraph 原生真流式——改用
        ``astream_events(version="v2")``，直接以 LLM token 为单位下发
        ``message``（首字延迟 = 首个 LLM token 的到达时间，而非全链路耗时）。

        事件类型（业务语义）：
        - ``metadata``：``{"status": "processing"}`` 首帧；以及可选的路由帧
          ``{"route": ..., "logic": ...}``（能从 ``analyze_and_route_query``
          节点输出拿到才发）
        - ``message``：``content`` 为一个 LLM token（仅「回答类」节点的非空
          content 才发；内部编排节点如路由/guardrails/planner/tool_selection
          的 token 一律不外泄）
        - ``tool_start`` / ``tool_end``：``metadata.tool`` 为工具名（检索进度）
        - ``done``：``metadata.sources`` 为引用来源（末尾恰好一次）
        - ``error``：友好的错误文案（异常不向调用方抛出）

        兜底：
        1. 若整条流一个 ``message`` 都没发出（过滤误杀，或该图根本没有流式
           LLM token），则在 ``done`` 之前从最外层图输出 ``messages[-1]``
           整段补发一个 ``message``——宁可退化成「整段返回」也不返回空内容；
        2. 无论正常结束还是异常，若此前未发出过 ``done``，都会在生成器返还
           前补发一次 ``done``，保证调用方（SSE 消费者）不会挂住。
        """
        # Phase 22：配置变更监测（与 _ask_impl 同款；见 config_watch 模块）。
        from agentix.application.services.config_watch import notice_if_changed

        notice_if_changed()

        # A1a：流式路径入控制面（Run source 统一——与 _invoke_tracked 同款
        # 登记语义）：spawn → bind(当前 SSE 消费任务) → running → 终态收口。
        # 登记放在**首帧之前**：准入拒绝（AdmissionDenied）在响应开始前
        # fail-closed 上抛，而不是流到一半才断连；登记故障降级为不追踪，
        # 不影响事件流（与 _invoke_tracked 的降级口径一致）。
        plane: Any = None
        run_id: Optional[str] = None

        def _finish_run(
            state: Optional[str] = None, error: Optional[str] = None
        ) -> None:
            """流式运行终态收口（幂等；已终态不被覆盖；故障不影响事件流）。"""
            if plane is None or run_id is None:
                return
            try:
                from agentix.runtime.control_plane import (
    RunState as _RunState,
)

                if state is None:
                    plane.finish(run_id)
                else:
                    plane.finish(run_id, state=_RunState(state), error=error)
            except Exception as exc:  # noqa: BLE001
                logger.debug("stream run finish skipped: {}", exc)

        try:
            from agentix.runtime.control_plane import (
    AdmissionDenied,
    get_control_plane,
)

            # Phase H · W3：准入三态（spawn 前；flag 关直通）——拒绝在首帧
            # 之前 fail-closed 上抛（落入下方既有 except → 429 映射）。
            from agentix.performance.admission import admission_wait

            await admission_wait(kind="chat_stream")
            plane = get_control_plane()
            run = plane.spawn(
                "kg_graph",
                trace_id=get_trace_id(),
                meta={"channel": "stream", "session_id": session_id},
            )
            run_id = run.run_id
            if run_scope is not None:
                from agentix.application.services.run_event_stream import (
                    RunEventEmitter,
                )

                # 登记成功：换装带 run_id 的发射器（首帧之前，故首帧起
                # 全部帧都带 run_id）
                run_scope["emitter"] = RunEventEmitter(run_id)
            current = asyncio.current_task()
            if current is not None:
                plane.bind(run_id, current)
            plane.mark_running(run_id)
        except AdmissionDenied as exc:
            logger.warning("control plane admission denied (stream): {}", exc)
            raise
        except Exception as exc:  # noqa: BLE001 - 登记失败不得阻断事件流
            logger.debug("control plane spawn skipped (stream): {}", exc)
            plane, run_id = None, None

        # 跨事件聚合状态：
        # - done_sent：是否已发出唯一的 done（兜底保证恰好一次）
        # - route_emitted：路由帧是否已发
        # - emitted_message：是否已转发过任何面向用户的 token（决定是否整段兜底）
        # - sources：引用来源
        done_sent = False
        route_emitted = False
        emitted_message = False
        sources: List[Any] = []
        sem_acquired = False
        # Phase 28（Ch10 §10.6）：budget_notice 本轮恰好一次（流式检查点去重）
        budget_notice_emitted = False
        # A1a：非抛异常的失败收口（idle/墙钟超时走 error 帧 + break，
        # 属于「失败完成」而非成功——终态不得记 SUCCEEDED）
        stream_failure: Optional[str] = None
        # A5/R5d：完成度校验所需的流内聚合（路由帧类型 + 已转发正文）
        route_type: Optional[str] = None
        answer_parts: List[str] = []

        def _record_stream_completion() -> None:
            """A5/R5d：流式**成功**终态补记完成度校验步骤（与 ask 同款确定性校验）。

            失败/超时路径（``stream_failure`` 非空）不记——与 ask 只在成功
            路径记 completion 的口径一致；校验器自身异常降级为不记。
            """
            if stream_failure is not None:
                return
            completion = _validate_completion_state(
                route=route_type,
                answer="".join(answer_parts),
                sources=sources,
                status=RequestStatus.OK.value,
            )
            if completion is not None:
                _record_completion_step(completion)

        try:
            # 首帧放 try 内：此刻起任何断连/取消都走下方终态收口，
            # 运行记录不会悬挂（此前首帧在 try 外，断连即泄漏 RUNNING）。
            yield {
                "type": "metadata",
                "metadata": {"status": "processing"},
                "session_id": session_id,
            }

            incremental_flag = (
                settings.INGEST_INCREMENTAL_DEFAULT
                if ingest_incremental is None
                else bool(ingest_incremental)
            )
            # P0-2(a)：图级最大轮数硬边界（同 ask()，config 形状只从 graph_config 出）
            config = thread_config(
                session_id,
                image_path=image_path,
                file_path=file_path,
                incremental=incremental_flag,
            )

            # 批次 7：长期记忆读回（同 _ask_impl；默认关，失败不影响事件流）
            memory_context = await _maybe_memory_context(message)
            if memory_context:
                config["configurable"]["memory_context"] = memory_context

            # 上下文工程 B3：指令分层块（同 _ask_impl；默认关 = 不触碰 config）。
            instructions = await _maybe_instructions(message)
            if instructions:
                config["configurable"]["instructions"] = instructions

            # P2：tool_policy 注入（同 _ask_impl；默认关 = 不触碰 config）。
            _inject_tool_policy(config)

            input_state = {
                "messages": [{"type": "human", "content": message}]
            }

            # 接口审查批次 2026-09-28：流式与 ask 同口径限流。此前流式路径
            # 完全绕过 _AGENT_SEM——AGENT_MAX_CONCURRENCY 在主用户路径上失效。
            # 槽位从首个事件前持有到流结束/异常/aclose（与非流式「整图执行
            # 期间占槽」同一语义），finally 统一归还。
            await _AGENT_SEM.acquire()
            sem_acquired = True

            # P0-2(a)：流式**不做整体 wait_for**——wait_for 只在整条协程
            # 返回/超时时才结算，期间 token 无法下发，会破坏增量语义
            # （首字延迟退化为全链路耗时）。改用**墙钟守卫**：每轮事件循环
            # 检查累计耗时，超限即通过既有 error 事件机制收口并 break，
            # 保持「恰好一次 done」的兜底契约。第一层超时由 llm_factory 的
            # per-LLM profile timeout（router 10s / general 45s …）兜底。
            import time as _time

            t0 = _time.perf_counter()
            # Phase 15 决策 A：stream **恒走 L2**（AgentLoop 无流式 API；
            # 合成伪事件 = 假 token 流）。AGENT_LOOP_ENABLED 只 gate 非流式 ask。
            # P1-4：LangGraph 原生事件流（graph_getter 惰性访问）
            # 批次 B1（audit-2026-09-28 §2 B1）：改**手动迭代**——每次取事件
            # 包 asyncio.wait_for(STREAM_IDLE_TIMEOUT)。原 async for 只有事件
            # 到达才执行循环体 ⇒ __anext__ 静默挂起（非 LLM 节点卡死/网络黑洞）
            # 时墙钟检查永不执行：信号量槽位被占、客户端悬挂。idle 超时与
            # 既有墙钟超时共用 error 收口，break 后由循环外 done 兜底 +
            # finally 归还槽位（恰好一次 done 契约不破）。
            stream_iter = self._graph_getter().astream_events(
                input_state, config=config, version="v2"
            ).__aiter__()
            while True:
                try:
                    event = await asyncio.wait_for(
                        stream_iter.__anext__(),
                        timeout=settings.STREAM_IDLE_TIMEOUT,
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    logger.error(
                        "Stream idle timeout exceeded ({}s)",
                        settings.STREAM_IDLE_TIMEOUT,
                    )
                    stream_failure = (
                        f"stream idle timeout after {settings.STREAM_IDLE_TIMEOUT}s"
                    )
                    # A5/R5d：超时属失败完成——补记首个错误步
                    _record_error_step("stream_timeout", stream_failure)
                    yield {
                        "type": "error",
                        "content": "处理请求超时，请稍后重试。",
                        "session_id": session_id,
                    }
                    break
                if _time.perf_counter() - t0 > settings.AGENT_TIMEOUT:
                    logger.error(
                        "Stream wall-clock timeout exceeded ({}s)",
                        settings.AGENT_TIMEOUT,
                    )
                    stream_failure = (
                        f"stream wall-clock timeout after {settings.AGENT_TIMEOUT}s"
                    )
                    # A5/R5d：超时属失败完成——补记首个错误步
                    _record_error_step("stream_timeout", stream_failure)
                    yield {
                        "type": "error",
                        "content": "处理请求超时，请稍后重试。",
                        "session_id": session_id,
                    }
                    break
                kind = event.get("event")
                name = event.get("name")
                data = event.get("data") or {}
                # P1-4(b)：节点级过滤依据（实测为裸节点名；缺省为 None）
                node_name = (event.get("metadata") or {}).get("langgraph_node")

                # Phase 28（Ch10 §10.6 预算意识·图主循环**流式**检查点）：
                # 根图 langgraph_step 逼近 recursion_limit（剩余 ≤2）⇒ 轨迹
                # append budget_notice（恰好一次；flag 关零成本；append-only
                # 不动静态前缀）。子图内部 step 独立计数 ⇒ 按
                # langgraph_checkpoint_ns 过滤（含 NS_SEP"|" 的非根任务不计）。
                # 非流式 graph.ainvoke 无步数观测点（LangGraph 内部强制）⇒ 不插
                # ——每个预算体系恰一处，防双写。
                if settings.BUDGET_NOTICE_ENABLED and not budget_notice_emitted:
                    _meta = event.get("metadata") or {}
                    _lg_step = _meta.get("langgraph_step")
                    _lg_ns = _meta.get("langgraph_checkpoint_ns")
                    if (
                        isinstance(_lg_step, int)
                        and isinstance(_lg_ns, str)
                        and "|" not in _lg_ns
                        and record_budget_notice(
                            steps_used=_lg_step,
                            max_steps=int(config.get("recursion_limit") or settings.MAX_ITERATIONS),
                        )
                    ):
                        budget_notice_emitted = True

                # 真 token 流：仅转发「回答类」节点的非空 chunk（不做 split/sleep）
                if kind == "on_chat_model_stream":
                    if self._is_answer_node(node_name):
                        content = self._extract_chunk_text(data.get("chunk"))
                        if content:
                            emitted_message = True
                            # A5/R5d：聚合正文供完成度校验
                            answer_parts.append(str(content))
                            yield {
                                "type": "message",
                                "content": content,
                                "session_id": session_id,
                            }

                # 工具（检索）进度：开始 / 结束
                elif kind == "on_tool_start":
                    # P2-2：把真实发生过的工具名累加进请求作用域（tools_used 数据源）
                    if name:
                        record_tool(str(name))
                        _record_budget_tool_call()
                    # F6：步骤级轨迹（工具开始）
                    _record_tool_step(str(name), "start", data)
                    yield {
                        "type": "tool_start",
                        "metadata": {"tool": name},
                        "session_id": session_id,
                    }
                elif kind == "on_tool_end":
                    # F6：步骤级轨迹（工具结束）
                    _record_tool_step(str(name), "end", data)
                    yield {
                        "type": "tool_end",
                        "metadata": {"tool": name},
                        "session_id": session_id,
                    }

                # 路由信息：能拿到就发一个 metadata 帧（拿不到不硬造）
                elif (
                    kind == "on_chain_end"
                    and name == "analyze_and_route_query"
                    and not route_emitted
                ):
                    output = data.get("output")
                    router_data = (
                        output.get("router") if isinstance(output, dict) else None
                    )
                    # router 节点真实返回的是 ``Router`` 模型实例（不是 dict），
                    # dict 与 LangChain BaseModel 都暴露 ``.get``。此前这里用
                    # ``isinstance(router_data, dict)`` 把关，生产路径恒为 False →
                    # 这一帧从来没发出去过，wire 上的 route 永远是 null。
                    if router_data is None:
                        route_type = route_logic = None
                    else:
                        route_type = router_data.get("type")
                        route_logic = router_data.get("logic")
                    if route_type:
                        route_emitted = True
                        yield {
                            "type": "metadata",
                            "metadata": {"route": route_type, "logic": route_logic},
                            "session_id": session_id,
                            "route": route_type,
                        }

                # 图执行结束（**仅最外层**）：提取 sources、整段兜底、发出唯一 done。
                #
                # 注意：子图作为节点嵌入时，子图自身也会发出 name=="LangGraph"
                # 的 on_chain_end（实测其 langgraph_node 为宿主节点名，如
                # "kb_workflow"）；只有最外层 LangGraph 的 langgraph_node 为 None。
                # 若不区分，会在子图结束时提前发 done，丢掉主图后续 token。
                elif (
                    kind == "on_chain_end"
                    and name == "LangGraph"
                    and node_name is None
                ):
                    output = data.get("output")
                    extracted = self._extract_sources(output)
                    if extracted:
                        sources = extracted
                    # 兜底：整条流一个 token 都没转发（过滤误杀 / 该图无流式 LLM）
                    # → 退化为整段返回，绝不让前端收到空内容
                    if not emitted_message:
                        whole_text = self._extract_final_text(output)
                        if whole_text:
                            emitted_message = True
                            # A5/R5d：聚合正文供完成度校验
                            answer_parts.append(str(whole_text))
                            yield {
                                "type": "message",
                                "content": whole_text,
                                "session_id": session_id,
                            }
                    # A5/R5d：成功终态补记完成度校验步骤
                    _record_stream_completion()
                    # F1b·M-P1-2：成功终态后 turn 结束评审（flag-off 零调度）
                    self._maybe_schedule_turn_review(
                        question=message, answer="".join(answer_parts)
                    )
                    yield {
                        "type": "done",
                        "metadata": {"sources": sources},
                        "session_id": session_id,
                    }
                    done_sent = True

            # 批次 B1：显式回收事件流（idle 超时已取消 __anext__、墙钟
            # break 后上游生成器仍挂起；正常结束时 aclose 为 no-op）。
            try:
                await stream_iter.aclose()
            except Exception:  # noqa: BLE001 - 清理失败不影响已产出的事件流
                logger.debug("stream aclose failed (non-fatal)", exc_info=True)

            # P1-4 兜底：未匹配到最外层图结束事件时补发 done（调用方不挂住）
            if not done_sent:
                # A5/R5d：成功（非失败）收尾同样补记完成度校验步骤
                _record_stream_completion()
                # F1b·M-P1-2：兜底成功终态同样触发 turn 结束评审
                self._maybe_schedule_turn_review(
                    question=message, answer="".join(answer_parts)
                )
                yield {
                    "type": "done",
                    "metadata": {"sources": sources},
                    "session_id": session_id,
                }
                done_sent = True

        except asyncio.CancelledError:
            # 非控制平面触发的取消（服务端关停等）：先收口终态再传播，
            # 保证运行记录不悬挂非终态（经 ControlPlane.cancel 的取消
            # 此时已是 CANCELLED，finish 幂等不覆盖）。
            _finish_run("cancelled", "cancelled")
            raise
        except GeneratorExit:
            # 客户端断连 / aclose：GeneratorExit 期间禁止 yield，只做
            # 终态收口后原样传播（断连的流不记 SUCCEEDED）。
            _finish_run("cancelled", "client_disconnected")
            raise
        except Exception as e:
            logger.error("Stream error: {}", e, exc_info=True)
            _finish_run("failed", f"{type(e).__name__}: {e}")
            # A5/R5d：异常路径补记首个错误步（与 ask 同口径：AgentError 用
            # 业务错误码，其余记 internal_error；失败路径不记 completion）
            _record_error_step(
                e.code.value if isinstance(e, AgentError) else "internal_error",
                f"{type(e).__name__}: {e}",
            )
            yield {
                "type": "error",
                "content": "处理请求时出错，请稍后重试。",
                "session_id": session_id,
            }
            # P1-4 兜底：异常路径同样补发 done，保证事件流有明确终止
            if not done_sent:
                yield {
                    "type": "done",
                    "metadata": {"sources": sources},
                    "session_id": session_id,
                }
                done_sent = True
        finally:
            # A1a 终态收口：正常结束（含 timeout break 的 error 帧路径，
            # 后者按 stream_failure 记 FAILED）未达终态则收口；cancel/异常
            # 分支先到者已生效（finish 幂等不覆盖）。
            if stream_failure is not None:
                _finish_run("failed", stream_failure)
            else:
                _finish_run()
            # 信号量归还（含异常与客户端 aclose 触发的 GeneratorExit 路径）
            if sem_acquired:
                _AGENT_SEM.release()


# 模块级单例（端点层通过 get_chat_service() 获取；测试可替换实例属性）
_service: Optional[ChatService] = None


def get_chat_service() -> ChatService:
    global _service
    if _service is None:
        _service = ChatService()
    return _service
