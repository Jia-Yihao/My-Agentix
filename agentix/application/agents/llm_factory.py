"""
LLM client factory - role-based model routing with timeout & retry.

依据《My Agentix_架构优化方案》P0-2(b)（专业化靠差异 [第10章]）：
不同角色用不同温度/超时/重试策略，而非全链路同一个 temperature=0.7。

**可插拔 Provider（P-扩展）**：LLM 供应商解析走
``infrastructure.llm.provider_registry`` 的注册表，而非硬编码 ``ChatOpenAI``。
新增 Provider 只需 ``llm_providers.register(name, builder)``，本模块调用链零改动。
内置 ``openai``（OpenAI 兼容端点，含 LM Studio / vLLM / DashScope）。

向后兼容：保留原 (temperature, tags) 位置参数签名，既有调用点无需改动。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional

from langchain_openai import ChatOpenAI
from loguru import logger

from agentix.config import settings
from agentix.infrastructure.llm.provider_registry import (
    DEFAULT_LLM_PROVIDER,
    llm_providers,
    resolve_llm_provider_name,
)

Role = Literal["router", "planner", "tool", "summarizer", "general", "hallucination"]

# 仅对**静态模板**角色上报 prompt 前缀指纹（general 角色的 system
# 消息含记忆/AGENTS 动态注入，逐请求变化属预期，不作漂移信号）——general 的
# 演进见下方 ``_TEMPLATE_PREFIX_ROLES``（指纹取模板常量）。
_STATIC_PROMPT_ROLES = frozenset({"router", "hallucination", "tool", "summarizer", "planner"})

# 上下文工程 A1：运行期内容动态、但**模板常量**静态的角色——指纹取模块级
# 提示词常量本身（而非本次 ainvoke 的 system 正文），既保留漂移信号，又不被
# 记忆/指令/路由 logic 注入打成逐请求抖动的噪声。
_TEMPLATE_PREFIX_ROLES = frozenset({"general"})


def _static_prompt_template(role: str) -> Optional[str]:
    """取该角色的静态提示词模板常量（``_TEMPLATE_PREFIX_ROLES`` 专用）。"""
    if role != "general":
        return None
    try:
        from agentix.application.agents.lg_prompts import GENERAL_QUERY_SYSTEM_PROMPT

        return GENERAL_QUERY_SYSTEM_PROMPT
    except Exception:  # noqa: BLE001 - 指标辅助提取绝不影响主链路
        return None


def _extract_cached_tokens(usage: object) -> Optional[int]:
    """取 provider 报告的**缓存命中** prompt tokens；未报告则 ``None``。

    OpenAI 兼容端点：``prompt_tokens_details.cached_tokens``；langchain 归一化后
    为 ``usage_metadata.input_token_details.cache_read``。两者任取其一，
    **字段缺失 = 供应商不支持/未启用缓存**，返回 ``None``（调用方据此不记
    miss，避免把「不支持」误算成「未命中」）。
    """
    if not isinstance(usage, dict):
        return None
    details = usage.get("input_token_details")
    if isinstance(details, dict) and "cache_read" in details:
        try:
            return int(details.get("cache_read") or 0)
        except (TypeError, ValueError):
            return None
    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, dict) and "cached_tokens" in prompt_details:
        try:
            return int(prompt_details.get("cached_tokens") or 0)
        except (TypeError, ValueError):
            return None
    return None


def _system_prompt_prefix(args: tuple, kwargs: dict) -> Optional[str]:
    """取首个 system 消息正文（dict 与 BaseMessage 两形态都吃），无则 None。"""
    messages = args[0] if args else kwargs.get("input")
    if messages is None or isinstance(messages, str):
        return None
    try:
        for message in messages:
            if isinstance(message, dict):
                if str(message.get("role", "")).lower() == "system":
                    content = message.get("content")
                    return content if isinstance(content, str) else None
                continue
            if str(getattr(message, "type", "") or "") == "system":
                content = getattr(message, "content", None)
                return content if isinstance(content, str) else None
    except Exception:  # noqa: BLE001 - 指标辅助提取绝不影响主链路
        return None
    return None


@dataclass(frozen=True)
class LLMProfile:
    """单一角色的模型配置。

    设计依据 [第10章]：专业化靠差异——路由/幻觉判定要确定性与低延迟，
    生成类保留创造性，而非全链路同一个 temperature=0.7。
    """

    role: Role
    model: str
    temperature: float
    timeout: float = 30.0
    max_retries: int = 2
    max_tokens: Optional[int] = None
    tags: List[str] = field(default_factory=list)


# 默认值：路由/幻觉判定要确定性与低延迟；生成类保留创造性
DEFAULT_PROFILES: Dict[Role, LLMProfile] = {
    "router": LLMProfile(
        "router", settings.OPENAI_MODEL, 0.0, timeout=10.0, max_retries=1, tags=["router"]
    ),
    "hallucination": LLMProfile(
        "hallucination",
        settings.OPENAI_MODEL,
        0.0,
        timeout=15.0,
        max_retries=1,
        tags=["hallucinations"],
    ),
    "planner": LLMProfile(
        "planner", settings.OPENAI_MODEL, 0.2, timeout=30.0, max_retries=2, tags=["research_plan"]
    ),
    "tool": LLMProfile(
        "tool", settings.OPENAI_MODEL, 0.0, timeout=20.0, max_retries=2, tags=["tool"]
    ),
    "summarizer": LLMProfile(
        "summarizer", settings.OPENAI_MODEL, 0.3, timeout=45.0, max_retries=2, tags=["summarize"]
    ),
    "general": LLMProfile(
        "general", settings.OPENAI_MODEL, 0.7, timeout=45.0, max_retries=2, tags=["general"]
    ),
}


class _TrackedChatOpenAI(ChatOpenAI):
    """带指标上报的 ChatOpenAI（P0-4b，零侵入包装）。

    接口与 ChatOpenAI 完全一致，仅在 ainvoke 外包一层 LLM 调用指标
    （token/延迟/成败）上报到 ObservabilityService（Redis），并追加一条
    步骤级轨迹（trajectory.py，支持「首个错误步」定位与完整回放）。
    注意：ChatOpenAI 是 pydantic 模型，附加属性用公开字段 role_tag
    而非下划线私有属性（pydantic v2 私有属性不能带默认值）。
    """

    role_tag: str = "general"

    async def ainvoke(self, *args, **kwargs):
        import time

        from agentix.application.services.observability import (
            LLMCallMetrics,
            get_observability,
        )
        from agentix.application.services.request_scope import record_llm_usage
        from agentix.domain.tracing import get_trace_id

        # 批次 13：token 预算的**先判断再调用**检查点。
        # 预算未启用（TOKEN_BUDGET_PER_REQUEST=0）时恒为 None ⇒ 零行为变化。
        try:
            from agentix.application.services.token_budget import budget_exhausted_error

            over_budget = budget_exhausted_error(agent=self.role_tag)
        except Exception:  # noqa: BLE001 - 预算设施故障不得影响主链路
            over_budget = None
        if over_budget is not None:
            raise over_budget

        t0 = time.perf_counter()
        ok, err, usage, res = True, None, {}, None
        try:
            # 企业化 P1-5.3：全局并发限制（max=0 时直通零开销；排队超时抛
            # LLMConcurrencyLimitError → 调用方走结构化错误路径）。
            from agentix.reliability.concurrency_limiter import get_llm_limiter
            # GenAI 语义 span（`chat {model}` + gen_ai.* 请求属性，
            # 用量在返回后补写；不写 trajectory——本方法下方已有
            # _record_llm_trajectory 步骤级轨迹，避免一步双记）。
            from agentix.observability.spans import apply_genai_usage, genai_span

            async with genai_span(self.model_name) as gen_span:
                async with get_llm_limiter().acquire():
                    res = await super().ainvoke(*args, **kwargs)
                apply_genai_usage(gen_span, usage=getattr(res, "usage_metadata", None))
            usage = getattr(res, "usage_metadata", None) or {}
            return res
        except BaseException as e:  # noqa: BLE001 - 指标需要记录所有失败
            ok, err = False, f"{type(e).__name__}: {e}"
            raise
        finally:
            # 指标上报失败绝不影响主链路（方案 P0-4 风险缓解）
            try:
                prompt_tokens = usage.get("input_tokens", 0) if isinstance(usage, dict) else 0
                completion_tokens = (
                    usage.get("output_tokens", 0) if isinstance(usage, dict) else 0
                )
                total_tokens = (
                    usage.get("total_tokens", 0) if isinstance(usage, dict) else 0
                )
                latency_ms = (time.perf_counter() - t0) * 1000

                # 企业化 P1-5.1：编排域 token 计量上报 Prometheus
                # （检索域已由 chat_llm.record_llm_usage 覆盖，此前编排域只进 Redis 不进指标）。
                # 独立 try + 置于 Redis 上报之前：Redis 故障不得连累指标出口。
                try:
                    from agentix.observability.metrics import record_llm_tokens

                    if prompt_tokens > 0:
                        record_llm_tokens(
                            model=self.model_name, direction="in", value=prompt_tokens
                        )
                    if completion_tokens > 0:
                        record_llm_tokens(
                            model=self.model_name, direction="out", value=completion_tokens
                        )
                except Exception:  # noqa: BLE001 - 指标失败不影响主链路
                    pass

                # 上下文工程 A2：provider 侧 KV/Prompt Cache 命中量（prompt 缓存
                # 命中率 = cached / prompt_tokens）。仅当供应商**明确报告**该字段
                # 才记账——字段缺失（不支持/未启用）不记 miss，避免假阴性。
                try:
                    cached_tokens = _extract_cached_tokens(usage)
                    if cached_tokens is not None:
                        from agentix.observability.metrics import (
                            record_cache_hit,
                            record_cache_miss,
                            record_llm_tokens,
                        )

                        if cached_tokens > 0:
                            record_llm_tokens(
                                model=self.model_name,
                                direction="in",
                                value=cached_tokens,
                                label_type="cached",
                            )
                        if prompt_tokens > 0:
                            if cached_tokens > 0:
                                record_cache_hit(cache_type="prompt_prefix")
                            else:
                                record_cache_miss(cache_type="prompt_prefix")
                except Exception:  # noqa: BLE001 - 指标失败不影响主链路
                    pass

                # 按角色的 LLM 调用/token 账本（role_tag 为有界
                # 枚举：router/planner/tool/summarizer/general/hallucination）。
                try:
                    from agentix.observability.metrics import (
                        record_agent_llm_call,
                        record_agent_llm_tokens,
                    )

                    record_agent_llm_call(
                        role=self.role_tag, status="success" if ok else "error"
                    )
                    if prompt_tokens > 0 or completion_tokens > 0:
                        record_agent_llm_tokens(
                            role=self.role_tag,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                        )
                except Exception:  # noqa: BLE001 - 指标失败不影响主链路
                    pass

                # 静态 system prompt 前缀指纹（同 role 指纹突变 =
                # 提示词漂移，Prometheus/Grafana 侧可见）。
                # 上下文工程 A1：静态角色取运行期 system 正文；general 正文含
                # 记忆/指令动态注入，改取**模板常量**作指纹——模板变更仍是漂移
                # 信号，且不逐请求抖动。
                try:
                    if self.role_tag in _STATIC_PROMPT_ROLES:
                        prefix = _system_prompt_prefix(args, kwargs)
                    elif self.role_tag in _TEMPLATE_PREFIX_ROLES:
                        prefix = _static_prompt_template(self.role_tag)
                    else:
                        prefix = None
                    if prefix:
                        from agentix.observability.metrics import (
                            record_prompt_prefix_fingerprint,
                        )

                        record_prompt_prefix_fingerprint(self.role_tag, prefix)
                except Exception:  # noqa: BLE001 - 指标失败不影响主链路
                    pass

                await get_observability().record_llm_call(
                    LLMCallMetrics(
                        model=self.model_name,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=total_tokens,
                        latency_ms=latency_ms,
                        success=ok,
                        error=err,
                        tags=[self.role_tag, get_trace_id()],
                    )
                )

                # P2-2：请求级聚合（与上面的 per-call 明细并存）。
                # 无活动 request_scope 时是静默 no-op；失败也绝不影响主链路。
                record_llm_usage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                )

                # F1/F6：步骤级轨迹（含可选的 prompt/response 脱敏预览）。
                _record_llm_trajectory(
                    role=self.role_tag,
                    model=self.model_name,
                    ok=ok,
                    err=err,
                    latency_ms=latency_ms,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    call_args=args,
                    call_kwargs=kwargs,
                    response=res,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("record_llm_call failed (non-fatal): {}", exc)


def _record_llm_trajectory(
    *,
    role: str,
    model: str,
    ok: bool,
    err: Optional[str],
    latency_ms: float,
    prompt_tokens: int,
    completion_tokens: int,
    call_args: tuple,
    call_kwargs: dict,
    response: object,
) -> None:
    """把一次 LLM 调用记为一条轨迹步骤（F1 完整回放 / F6 首个错误步定位）。

    payload 预览（prompt / response）仅在 ``OBSERVABILITY_PAYLOAD_CAPTURE=True``
    时捕获，且经脱敏 + 截断——默认关闭以保护隐私与存储。
    """
    from agentix.application.services.trajectory import (
        get_trajectory_recorder,
        payload_capture_enabled,
        redact_and_truncate,
    )

    detail: Dict[str, object] = {
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }
    if payload_capture_enabled():
        try:
            messages = call_args[0] if call_args else call_kwargs.get("input")
            if messages is not None:
                detail["prompt_preview"] = redact_and_truncate(messages)
            if response is not None:
                content = getattr(response, "content", response)
                detail["response_preview"] = redact_and_truncate(content)
        except Exception as exc:  # noqa: BLE001 - 预览失败不影响轨迹
            logger.debug("payload preview skipped: {}", exc)

    get_trajectory_recorder().record(
        kind="llm",
        name=role,
        status="ok" if ok else "error",
        latency_ms=latency_ms,
        detail=detail,
        error=err,
    )


def _build_openai_client(
    profile: LLMProfile,
    *,
    temperature: float,
    tags: Optional[List[str]],
    timeout: Optional[float],
    max_retries: Optional[int],
) -> ChatOpenAI:
    """内置 ``openai`` Provider 构建函数（OpenAI 兼容端点）。

    独立成函数以便注册进 ``llm_providers``；参数语义与原 ``get_llm_client``
    实现逐字一致（含 ``temperature != 0.7`` sentinel 的向后兼容技巧）。
    """
    return _TrackedChatOpenAI(
        openai_api_key=settings.OPENAI_API_KEY,
        model_name=profile.model,
        openai_api_base=settings.OPENAI_API_BASE,
        temperature=temperature if temperature != 0.7 else profile.temperature,
        max_tokens=profile.max_tokens,
        timeout=timeout if timeout is not None else profile.timeout,
        max_retries=max_retries if max_retries is not None else profile.max_retries,
        tags=tags or profile.tags,
        role_tag=profile.role,
    )


def _parse_role_tier_map(raw: str) -> Dict[str, str]:
    """解析 LLM_ROLE_TIER_MAP（形如 router=fast,planner=reasoning）到 dict。

    坏项（缺等号、空 role/tier）丢弃并记 warning，配置写错不打挂建链。
    """
    mapping: Dict[str, str] = {}
    for part in (raw or "").split(","):
        entry = part.strip()
        if not entry:
            continue
        if "=" not in entry:
            logger.warning(
                "LLM_ROLE_TIER_MAP: ignoring malformed entry {!r} (want role=tier)", entry
            )
            continue
        role_name, tier = (s.strip() for s in entry.split("=", 1))
        if role_name and tier:
            mapping[role_name] = tier.lower()
    return mapping


def _apply_tier_routing(profile: LLMProfile) -> LLMProfile:
    """角色到档位接线（LLM_TIER_ROUTING_ENABLED，默认关）。

    flag-off / 空表 / 未命中角色 / 未知档位 / 解析异常 => 原 profile 原样返回
    （与基线逐位等价，fail-open 不打挂建链）；命中 => 经既有
    ModelGateway.resolve_model 取该档模型（档位模型空 = 回退 LLM_MODEL）。
    """
    if not bool(getattr(settings, "LLM_TIER_ROUTING_ENABLED", False)):
        return profile
    raw_map = str(getattr(settings, "LLM_ROLE_TIER_MAP", "") or "")
    if not raw_map.strip():
        return profile
    tier = _parse_role_tier_map(raw_map).get(profile.role)
    if not tier:
        return profile
    try:
        from agentix.infrastructure.llm.model_gateway import get_model_gateway

        tier_model = get_model_gateway().resolve_model(tier)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 - 档位解析失败回退基线 profile
        logger.warning(
            "tier routing resolve failed for role={} tier={}: {}; falling back to base profile",
            profile.role,
            tier,
            exc,
        )
        return profile
    if not tier_model or tier_model == profile.model:
        return profile
    logger.info(
        "tier routing: role={} -> tier={} -> model={}", profile.role, tier, tier_model
    )
    return LLMProfile(
        role=profile.role,
        model=tier_model,
        temperature=profile.temperature,
        timeout=profile.timeout,
        max_retries=profile.max_retries,
        max_tokens=profile.max_tokens,
        tags=list(profile.tags),
    )


def ensure_llm_providers_registered() -> None:
    """幂等注册内置 LLM Provider（首次调用时执行）。

    设计说明：内置 Provider 的构建函数位于本模块（application 层），而注册表
    位于 infrastructure 层。为保持依赖方向（application → infrastructure 容器，
    不反向），由 application 侧在首次解析前主动注册，而非 infrastructure 反向
    import application。
    """
    if not llm_providers.has(DEFAULT_LLM_PROVIDER):
        llm_providers.register(DEFAULT_LLM_PROVIDER, _build_openai_client)


def get_llm_client(
    temperature: float = 0.7,
    tags: Optional[List[str]] = None,
    *,
    role: Role = "general",
    timeout: Optional[float] = None,
    max_retries: Optional[int] = None,
    provider: Optional[str] = None,
) -> ChatOpenAI:
    """Create a chat model client with shared configuration.

    向后兼容：保留原有 (temperature, tags) 位置参数，老调用点无需改动。
    新增 role/timeout/max_retries 关键字参数，按角色分级施加
    超时与重试硬边界（P0-2）。

    Args:
        temperature: LLM temperature for generation（显式传入非默认值时优先生效）.
        tags: Optional tags for tracing/logging.
        role: 角色名，用于选择预设 profile（温度/超时/重试分级）.
        timeout: 显式覆盖 profile 超时（秒）.
        max_retries: 显式覆盖 profile 重试次数.
        provider: 显式指定 Provider 名；缺省读取 ``settings.LLM_PROVIDER``
            （未配置时回退 ``openai``）。未注册的 Provider 抛
            ``RegistryError``（fail-fast，不静默退回默认实现）。

    Returns:
        Configured chat model instance（P0-4: 带指标上报的包装实例）.

    Raises:
        RuntimeError: OPENAI_API_KEY 未配置时（openai Provider）.
        RegistryError: 指定的 Provider 未注册时.
    """
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured.")

    profile = DEFAULT_PROFILES.get(role, DEFAULT_PROFILES["general"])
    # 角色到档位接线（LLM_TIER_ROUTING_ENABLED，默认关时恒原样返回）。
    profile = _apply_tier_routing(profile)

    ensure_llm_providers_registered()
    provider_name = resolve_llm_provider_name(
        provider if provider is not None else getattr(settings, "LLM_PROVIDER", None)
    )
    builder = llm_providers.factory(provider_name)  # 未注册 → RegistryError
    return builder(
        profile,
        temperature=temperature,
        tags=tags,
        timeout=timeout,
        max_retries=max_retries,
    )
