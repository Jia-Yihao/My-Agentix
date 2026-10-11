"""工具护栏策略（M3 迁入，自 capabilities/tools/guardrails.py）。
"""
from __future__ import annotations

from agentix.policies.guardrails.guardrails import (
    IDENTICAL_RESULT_WARN,
    SAME_ARGS_BLOCK,
    SAME_ARGS_HALT,
    SAME_ARGS_WARN,
    TOOL_FAILURES_BLOCK,
    TOOL_FAILURES_HALT,
    TOOL_FAILURES_WARN,
    GuardrailTier,
    GuardrailVerdict,
    ToolGuardrails,
    args_fingerprint,
    get_tool_guardrails,
    recovery_advice,
    reset_tool_guardrails,
)

__all__ = [
    "GuardrailTier",
    "GuardrailVerdict",
    "ToolGuardrails",
    "recovery_advice",
    "args_fingerprint",
    "get_tool_guardrails",
    "reset_tool_guardrails",
    "SAME_ARGS_WARN",
    "SAME_ARGS_BLOCK",
    "SAME_ARGS_HALT",
    "TOOL_FAILURES_WARN",
    "TOOL_FAILURES_BLOCK",
    "TOOL_FAILURES_HALT",
    "IDENTICAL_RESULT_WARN",
]
