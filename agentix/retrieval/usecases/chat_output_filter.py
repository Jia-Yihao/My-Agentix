"""答案输出过滤薄封装（W1 / T17）：答案落库前过 OutputFilter。

行为档位（保守，避免误杀）：
- PII 脱敏（默认开）：用脱敏文本替换原答案，透明处理；
- 有害内容（默认关）：命中替换为固定提示 + 指标 + WARN；
- 幻觉检测（默认关，观察模式）：命中仅记指标 + WARN，不改写答案。

总开关 ``OUTPUT_FILTER_ENABLED`` 关或三项全关时直接原样返回（零开销）。
"""
from __future__ import annotations

import logging

from agentix.observability.metrics import record_output_filter_blocked
from agentix.retrieval.settings import settings

logger = logging.getLogger(__name__)

_HARM_BLOCK_HINT = "回复内容包含不适宜信息，已被安全过滤。"


def _all_detection_off() -> bool:
    return not (
        settings.OUTPUT_FILTER_PII_ENABLED
        or settings.OUTPUT_FILTER_HARM_ENABLED
        or settings.OUTPUT_FILTER_HALLUCINATION_ENABLED
    )


async def filter_answer(
    answer: str,
    *,
    contexts: list[str],
    question: str,
) -> tuple[str, dict]:
    """过滤最终答案，返回 (最终文本, 过滤信息 dict)。"""
    # 企业化 P1-4.2 ③：输出侧注入痕迹 observe-only 复扫（独立于 OUTPUT_FILTER_ENABLED，
    # 只受 PROMPT_INJECTION_DETECTION_ENABLED 控制；只记数告警，不改写答案）
    from agentix.config.settings import settings as main_settings

    if main_settings.PROMPT_INJECTION_DETECTION_ENABLED and answer:
        try:
            from agentix.observability.metrics import record_prompt_injection
            from agentix.security.prompt_injection import detect_prompt_injection

            findings = detect_prompt_injection(answer)
            if findings:
                record_prompt_injection("output")
                logger.warning(
                    "injection traces in final answer (observe mode, answer kept): %s",
                    sorted({f.label for f in findings}),
                    extra={"stage": "chat.output_injection_scan", "status": "observed"},
                )
        except Exception:  # noqa: BLE001 - 观察扫描绝不影响主链路
            pass

    if not settings.OUTPUT_FILTER_ENABLED or not answer or _all_detection_off():
        return answer, {"blocked": False}

    from agentix.security.output_filter import OutputFilter

    output_filter = OutputFilter(
        enable_pii_masking=settings.OUTPUT_FILTER_PII_ENABLED,
        enable_harm_detection=settings.OUTPUT_FILTER_HARM_ENABLED,
        enable_hallucination_detection=settings.OUTPUT_FILTER_HALLUCINATION_ENABLED,
    )
    result = await output_filter.filter(answer, context=contexts, question=question)
    info = {
        "blocked": result.blocked,
        "reason": result.reason,
        "pii": list(result.pii_detected),
        "hallucination_score": result.hallucination_score,
    }

    # 有害内容：命中替换为固定提示
    if result.harm_detected:
        record_output_filter_blocked("harm")
        logger.warning(
            "answer blocked by harm detection",
            extra={"stage": "chat.output_filter", "status": "blocked", "error_code": "harm"},
        )
        return _HARM_BLOCK_HINT, {**info, "blocked": True}

    # 幻觉检测：观察模式，仅记指标与 WARN，不改写答案
    if result.blocked and settings.OUTPUT_FILTER_HALLUCINATION_ENABLED:
        record_output_filter_blocked("hallucination")
        logger.warning(
            "hallucination suspected (observe mode, answer kept)",
            extra={"stage": "chat.output_filter", "status": "observed"},
        )
        return answer, {**info, "blocked": False}

    # PII：用脱敏文本替换原答案
    if result.pii_detected:
        logger.info(
            "answer pii masked",
            extra={"stage": "chat.output_filter", "status": "pii_masked"},
        )
        return result.filtered_text, info

    return answer, info
