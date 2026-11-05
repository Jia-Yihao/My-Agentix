"""
对话用 LLM 配置：DeepSeek V4-Flash + 快速/深度模式。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Literal

from langchain_openai import ChatOpenAI

from agentix.retrieval.config import get_default_config
from agentix.retrieval.settings import settings

ChatMode = Literal["fast", "deep"]

DEFAULT_CHAT_MODEL = "deepseek-v4-flash"


def resolve_chat_model(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    cfg = get_default_config()
    return os.getenv("CHAT_MODEL") or cfg.get("model") or DEFAULT_CHAT_MODEL


def thinking_extra_body(chat_mode: ChatMode) -> dict[str, Any]:
    """Agent 主链路固定关闭 thinking（避免与 tool call 冲突）；深度思考由独立流式调用展示。"""
    if chat_mode == "deep":
        return {"thinking": {"type": "disabled"}}
    return {"thinking": {"type": "disabled"}}


def deep_thinking_extra_body() -> dict[str, Any]:
    return {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}


def create_chat_llm(
    *,
    model_name: str | None = None,
    chat_mode: ChatMode = "fast",
    for_thinking_stream: bool = False,
    temperature: float | None = None,
) -> ChatOpenAI:
    cfg = get_default_config()
    model = resolve_chat_model(model_name)
    extra = deep_thinking_extra_body() if for_thinking_stream else thinking_extra_body(chat_mode)
    return ChatOpenAI(
        model=model,
        temperature=temperature if temperature is not None else cfg.get("temperature", 0.1),
        timeout=cfg.get("llm_timeout", 60 if for_thinking_stream else 30),
        max_retries=cfg.get("llm_max_retries", 3),
        api_key=cfg["llm_api_key"],
        base_url=cfg["llm_base_url"],
        extra_body=extra,
    )


async def invoke_with_breaker(llm, prompt, *, name: str = "llm.chat") -> Any:
    """经熔断器执行 LLM 调用（W2 / T18；W7 加 GenAI 语义 span）。

    - ``LLM_CIRCUIT_BREAKER_ENABLED=false`` 时直连（零开销）；
    - 开启时经 ``CircuitBreakerRegistry`` 按名取熔断器：OPEN 抛
      ``CircuitBreakerOpenError``；调用在线程中执行（不阻塞事件循环），
      成功/失败分别 ``record_success`` / ``record_failure``；
    - 外层 ``genai_span``（``chat {model}`` + ``gen_ai.*`` 请求/
      用量属性；不写 trajectory——检索域 usage 已有 DB/M6 记录）。
    """
    from agentix.observability.spans import apply_genai_usage, genai_span

    model = getattr(llm, "model_name", None) or getattr(llm, "model", None) or None
    async with genai_span(model) as gen_span:
        if not settings.LLM_CIRCUIT_BREAKER_ENABLED:
            # 企业化 P1-5.3：全局并发限制（max=0 时直通零开销）
            from agentix.reliability.concurrency_limiter import get_llm_limiter

            async with get_llm_limiter().acquire():
                result = await asyncio.to_thread(llm.invoke, prompt)
        else:
            from agentix.reliability.circuit_breaker import registry as breaker_registry

            breaker = breaker_registry.get_or_create(
                name,
                failure_threshold=settings.LLM_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
                recovery_timeout=settings.LLM_CIRCUIT_BREAKER_RECOVERY_SECONDS,
            )

            from agentix.reliability.concurrency_limiter import get_llm_limiter

            async def _call() -> Any:
                async with get_llm_limiter().acquire():
                    return await asyncio.to_thread(llm.invoke, prompt)

            result = await breaker.call(_call)

        apply_genai_usage(gen_span, usage=getattr(result, "usage_metadata", None))
        return result


# -- M6: 成本计量 --------------------------------------------------------


def extract_usage(resp) -> tuple[int, int, bool]:
    """从 LLM 响应提取 token 用量。

    Returns:
        (tokens_in, tokens_out, estimated)
        - estimated=True 表示 LLM 未返回 usage，按字符数估算回退。
    """
    usage = getattr(resp, "usage_metadata", None) or getattr(resp, "response_metadata", {}).get("token_usage", {})
    if isinstance(usage, dict) and (usage.get("input_tokens") or usage.get("output_tokens")):
        return (
            int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0),
            int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0),
            False,
        )
    # 估算回退：按字符数估算
    content = getattr(resp, "content", None) or str(resp)
    chars_per_token = max(1.0, settings.LLM_ESTIMATE_CHARS_PER_TOKEN)
    est_out = int(len(str(content)) / chars_per_token)
    return (0, est_out, True)


def calculate_cost(tokens_in: int, tokens_out: int) -> float:
    """成本 = input_tokens * 输入单价 + output_tokens * 输出单价（每百万 token）。"""
    cost = (
        tokens_in * settings.LLM_PRICE_INPUT_PER_1M
        + tokens_out * settings.LLM_PRICE_OUTPUT_PER_1M
    ) / 1_000_000
    return round(cost, 6)


def record_llm_usage(
    session,
    *,
    request_id: str,
    model_name: str,
    tokens_in: int,
    tokens_out: int,
    branch: str = "chat_stream",
    estimated: bool = False,
    kb_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """记录 LLM usage 到 DB + 指标（M6.1/M6.3）。不阻断主流程。"""
    try:
        from agentix.retrieval.domain.models.ops import UsageRecord
        from agentix.observability.metrics import (
            record_agent_llm_call,
            record_agent_llm_tokens,
            record_llm_tokens,
        )

        cost = calculate_cost(tokens_in, tokens_out)
        row = UsageRecord(
            request_id=request_id,
            kb_id=kb_id,
            user_id=user_id,
            action=f"llm.{branch}",
            latency_ms=0,
            tokens_in=max(0, int(tokens_in)),
            tokens_out=max(0, int(tokens_out)),
            cost=cost,
            model_name=model_name or "",
            branch=branch,
            estimated=estimated,
        )
        session.add(row)
        session.commit()
        record_llm_tokens(model=model_name or "default", direction="in", value=tokens_in)
        record_llm_tokens(model=model_name or "default", direction="out", value=tokens_out)
        # 检索域回答链路的按角色 LLM 账本（role=retrieval）。
        record_agent_llm_call(role="retrieval", status="success")
        record_agent_llm_tokens(
            role="retrieval", prompt_tokens=tokens_in, completion_tokens=tokens_out
        )
    except Exception:
        import logging

        logging.getLogger(__name__).warning("record_llm_usage failed", exc_info=True)
        try:
            session.rollback()
        except Exception:
            pass
