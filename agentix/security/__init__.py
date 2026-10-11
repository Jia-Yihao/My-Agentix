"""
安全模块

提供输出过滤、PII 脱敏、有害内容检测、幻觉检测等安全能力。
JWT 认证工具（签发/校验 access_token）。

使用方式：
    from agentix.security import OutputFilter, FilterResult, PIIMasker
    from agentix.security import create_access_token, decode_access_token, UserContext
"""

from agentix.security.output_filter import (
    OutputFilter,
    FilterResult,
    PIIMasker,
    HarmfulContentDetector,
    HarmCategory,
    HallucinationDetector,
    HallucinationResult,
)
from agentix.security.jwt import (
    UserContext,
    create_access_token,
    decode_access_token,
    safe_decode_access_token,
)
from agentix.security.prompt_injection import (
    DATA_BOUNDARY_CLOSE,
    DATA_BOUNDARY_OPEN,
    InjectionFinding,
    build_boundary_notice,
    detect_prompt_injection,
    scan_chunks_for_injection,
    wrap_document_content,
)

__all__ = [
    "OutputFilter",
    "FilterResult",
    "PIIMasker",
    "HarmfulContentDetector",
    "HarmCategory",
    "HallucinationDetector",
    "HallucinationResult",
    "UserContext",
    "create_access_token",
    "decode_access_token",
    "safe_decode_access_token",
    # 企业化 P1-4.2：prompt injection 防线
    "DATA_BOUNDARY_CLOSE",
    "DATA_BOUNDARY_OPEN",
    "InjectionFinding",
    "build_boundary_notice",
    "detect_prompt_injection",
    "scan_chunks_for_injection",
    "wrap_document_content",
]
