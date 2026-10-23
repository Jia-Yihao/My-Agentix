"""工具层异常闭环护栏（P4-4.1，借鉴 hermes ``tool_guardrails.py``）。

《My-Agentix 项目说明书》岗位对照·Planning/ReACT/Tool 调用/回滚重试缺口：
源侧在工具失败后有**三计数器分级**与**工具专属恢复建议回喂**，本仓此前
只有「失败转数据」，没有「失败后系统性变聪明」。

**三计数器**（判级输入，按工具聚合）：

1. ``same_args_failures`` —— **相同参数**的连续失败（同参连打没有信息量，
   升级最快）；
2. ``tool_failures`` —— 该工具的失败总数（自上次清零；不同参数也算——
   「这个工具在当前环境下就是跑不通」）；
3. ``identical_result_repeats`` —— **幂等结果哈希不变**的连续次数（成功但
   结果字节级不变，通常是轮询/空转信号）。

**判级**（``GuardrailTier``）：``warn`` 附加恢复建议 → ``block`` 拒绝**同参**
再执行 → ``halt`` 拒绝该工具全部执行（直到 mutation 成功或 ``reset()``）。

**清零**（源 :418-422 语义）：发生过一次**成功的 mutation**
（``ToolSpec.side_effect=True`` 且成功）即把该工具的计数/拦截全部清零；
只读成功只打断「同参连续」链（tool_failures 保留——读成功不能证明写通了）。

**恢复建议**（``recovery_advice``）：按错误文本确定性分型（超时/权限/
参数/名称/上游瞬时/未知），产出**工具专属、可执行**的一句话，由调用方
回喂给模型或写进失败观测——建议文本刻意**不含** ``is_retryable`` 的
ASCII 提示词（domain/policies/error_classification.py），避免拼接后
把不可重试错误误判成可重试。

**flag 纪律**：``settings.TOOL_GUARDRAILS_ENABLED`` 默认 ``False``；
所有接线点（executor / replanner / task_retry / 结果出层）都在 flag 分支
内调用本模块——flag-off 时本模块一次都不会被调用，与基线逐位等价。
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

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

# ---------------------------------------------------------------------------
# 判级阈值（确定性常量；调档即行为变更，须过 p54 契约测试）
# ---------------------------------------------------------------------------

#: 同参连续失败 → warn / block / halt
SAME_ARGS_WARN = 2
SAME_ARGS_BLOCK = 3
SAME_ARGS_HALT = 5

#: 工具级失败总数 → warn / block / halt
TOOL_FAILURES_WARN = 2
TOOL_FAILURES_BLOCK = 5
TOOL_FAILURES_HALT = 10

#: 幂等结果哈希不变（连续）→ warn（仅建议，永不 block/halt）
IDENTICAL_RESULT_WARN = 2

#: 跟踪工具数上限（防长跑进程内存泄漏；LRU 淘汰最旧）
MAX_TRACKED_TOOLS = 128


class GuardrailTier(str, Enum):
    """护栏判级：``ok`` 放行，``warn`` 放行+建议，``block``/``halt`` 拒绝。"""

    OK = "ok"
    WARN = "warn"
    BLOCK = "block"
    HALT = "halt"


@dataclass(frozen=True)
class GuardrailVerdict:
    """一次判级结果（不可变；供 metadata 序列化与接线点消费）。"""

    tier: GuardrailTier
    tool: str
    advice: str = ""
    same_args_failures: int = 0
    tool_failures: int = 0

    @property
    def rejected(self) -> bool:
        """是否拒绝执行（block / halt）。"""
        return self.tier in (GuardrailTier.BLOCK, GuardrailTier.HALT)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.tier.value,
            "tool": self.tool,
            "advice": self.advice,
            "same_args_failures": self.same_args_failures,
            "tool_failures": self.tool_failures,
        }


def args_fingerprint(args: Optional[Dict[str, Any]]) -> str:
    """参数指纹：确定性序列化后取 sha256（键排序，跨 dict 书写顺序稳定）。"""
    try:
        blob = json.dumps(args or {}, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - default=str 兜底后极难触发
        blob = str(args)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _result_hash(value: Any) -> str:
    """结果字节级哈希（与 result_limiter._serialize 同口径的 JSON 序列化）。"""
    try:
        blob = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover
        blob = str(value)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 恢复建议（输出刻意避开 is_retryable 的 ASCII 提示词，见模块 docstring）
# ---------------------------------------------------------------------------

#: 错误文本关键词 → 建议生成器（顺序敏感；先匹配者胜）
_ADVICE_RULES = (
    (
        ("timeout", "timed out", "deadline"),
        "缩小单次调用范围或拆成更小步骤；确需长跑时调高该工具的超时上限后重试，勿原样连打",
    ),
    (
        ("permission", "denied", "forbidden", "review", "审批", "403"),
        "这是权限/审批闸门拒绝：改用只读等价工具，或先完成人工审批再执行；原样重试不会放行",
    ),
    (
        ("not found", "unknown tool", "no such", "tool_not_found"),
        "目标名称不存在：核对工具名与参数取值是否在当前可用清单内，再重试",
    ),
    (
        ("invalid", "argument", "valueerror", "typeerror", "missing", "schema", "400"),
        "入参不规范：对照该工具的参数 schema 逐项核对类型与必填项后重试",
    ),
    (
        ("rate limit", "429", "503", "502", "504", "upstream", "overloaded", "connection"),
        "上游瞬时不可用：退避片刻再试，本轮内不要原样连续重放同一调用",
    ),
)

_DEFAULT_ADVICE = "换一组参数重试；若仍失败，改用其他工具完成同一目标步骤"


def recovery_advice(tool: str, error: str) -> str:
    """按错误文本产出**工具专属**恢复建议（确定性，零 LLM）。

    建议是给模型看的行动指令（回喂），不是日志——每条都以「怎么做」收尾；
    未命中任何分型时给保守默认（换参 / 换工具），不编造具体操作。
    """
    msg = f"{tool}: {error or ''}".lower()
    for keys, advice in _ADVICE_RULES:
        if any(k in msg for k in keys):
            return advice
    return _DEFAULT_ADVICE


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


@dataclass
class _ToolCounters:
    """单工具计数（有界：仅存计数与指纹，不存结果正文）。"""

    last_fp: Optional[str] = None
    same_args_failures: int = 0
    tool_failures: int = 0
    last_result_hash: Optional[str] = None
    identical_result_repeats: int = 0
    last_error: str = ""
    blocked_fps: set = field(default_factory=set)
    halted: bool = False


class ToolGuardrails:
    """三计数器失败分级 + 恢复建议 + mutation 清零（进程内、有界）。

    实例自身**不读 settings**——开关由接线点判断（flag-off 时零调用），
    便于测试直接注入与并行实例化。
    """

    def __init__(self, *, max_tools: int = MAX_TRACKED_TOOLS) -> None:
        self._max_tools = max(1, int(max_tools))
        self._tools: "OrderedDict[str, _ToolCounters]" = OrderedDict()

    # -- 内部 ---------------------------------------------------------------

    def _counters(self, tool: str) -> _ToolCounters:
        counters = self._tools.get(tool)
        if counters is None:
            counters = _ToolCounters()
        else:
            self._tools.move_to_end(tool)
        self._tools[tool] = counters
        while len(self._tools) > self._max_tools:
            self._tools.popitem(last=False)
        return counters

    @staticmethod
    def _tier_for(counters: _ToolCounters) -> GuardrailTier:
        if (
            counters.same_args_failures >= SAME_ARGS_HALT
            or counters.tool_failures >= TOOL_FAILURES_HALT
        ):
            return GuardrailTier.HALT
        if (
            counters.same_args_failures >= SAME_ARGS_BLOCK
            or counters.tool_failures >= TOOL_FAILURES_BLOCK
        ):
            return GuardrailTier.BLOCK
        if (
            counters.same_args_failures >= SAME_ARGS_WARN
            or counters.tool_failures >= TOOL_FAILURES_WARN
            or counters.identical_result_repeats >= IDENTICAL_RESULT_WARN
        ):
            return GuardrailTier.WARN
        return GuardrailTier.OK

    # -- 前置检查（执行链最前）---------------------------------------------

    def check(
        self, tool: str, args: Optional[Dict[str, Any]] = None
    ) -> Optional[GuardrailVerdict]:
        """执行前拦截判定：``None`` = 放行；返回 verdict 且 ``rejected`` = 拒绝。

        - 工具已 ``halt`` ⇒ 拒绝一切调用；
        - 本组参数指纹已在 ``block`` 集合 ⇒ 拒绝**同参**再执行
          （换一组参数仍可试——「同参连打」才是无信息量动作）。
        """
        counters = self._tools.get(str(tool))
        if counters is None:
            return None
        fp = args_fingerprint(args)
        tier: Optional[GuardrailTier] = None
        if counters.halted:
            tier = GuardrailTier.HALT
        elif fp in counters.blocked_fps:
            tier = GuardrailTier.BLOCK
        if tier is None:
            return None
        return GuardrailVerdict(
            tier=tier,
            tool=str(tool),
            advice=recovery_advice(str(tool), f"guardrail {tier.value}"),
            same_args_failures=counters.same_args_failures,
            tool_failures=counters.tool_failures,
        )

    # -- 失败记账 ------------------------------------------------------------

    def record_failure(
        self, tool: str, args: Optional[Dict[str, Any]] = None, *, error: str = ""
    ) -> GuardrailVerdict:
        """记一次工具失败并判级（接线点：执行链最终失败出口）。

        判级副作用写进计数器：``block`` 把本组参数指纹入拦截集合，
        ``halt`` 置工具级熔断——后续 :meth:`check` 据此拒绝。
        """
        name = str(tool)
        counters = self._counters(name)
        fp = args_fingerprint(args)
        if fp == counters.last_fp:
            counters.same_args_failures += 1
        else:
            counters.last_fp = fp
            counters.same_args_failures = 1
        counters.tool_failures += 1
        counters.last_error = error or counters.last_error

        tier = self._tier_for(counters)
        if tier == GuardrailTier.BLOCK:
            counters.blocked_fps.add(fp)
        elif tier == GuardrailTier.HALT:
            counters.halted = True
        advice = recovery_advice(name, error) if tier != GuardrailTier.OK else ""
        return GuardrailVerdict(
            tier=tier,
            tool=name,
            advice=advice,
            same_args_failures=counters.same_args_failures,
            tool_failures=counters.tool_failures,
        )

    # -- 成功记账（含 mutation 清零）----------------------------------------

    def record_success(
        self,
        tool: str,
        args: Optional[Dict[str, Any]] = None,
        *,
        result: Any = None,
        mutation: bool = False,
    ) -> GuardrailVerdict:
        """记一次工具成功。

        - ``mutation=True``（``ToolSpec.side_effect=True`` 的成功）⇒ **全清**
          （计数/拦截/熔断一次性归零——源 :418-422「成功 mutation 即清零」）；
        - 只读成功 ⇒ 仅打断「同参连续失败」链，``tool_failures`` 保留；
        - 结果哈希与上次相同 ⇒ ``identical_result_repeats`` 累加（≥ 阈值出
          warn 建议；**轮询不豁免**——不变的幂等结果正是要被提醒的信号），
          哈希变化则归零。
        """
        name = str(tool)
        counters = self._counters(name)
        if mutation:
            counters.last_fp = None
            counters.same_args_failures = 0
            counters.tool_failures = 0
            counters.identical_result_repeats = 0
            counters.last_result_hash = None
            counters.last_error = ""
            counters.blocked_fps.clear()
            counters.halted = False
            return GuardrailVerdict(tier=GuardrailTier.OK, tool=name)

        counters.same_args_failures = 0  # 任意成功都打断「同参连续失败」链
        h = _result_hash(result)
        if h == counters.last_result_hash:
            counters.identical_result_repeats += 1
        else:
            counters.last_result_hash = h
            counters.identical_result_repeats = 0

        tier = self._tier_for(counters)
        advice = ""
        if tier == GuardrailTier.WARN and counters.identical_result_repeats >= IDENTICAL_RESULT_WARN:
            advice = (
                "结果与上次字节级相同：确认这是否为预期轮询；否则更换参数"
                "或查询条件以取得新信息"
            )
        elif tier == GuardrailTier.WARN:
            advice = recovery_advice(name, "repeated")
        return GuardrailVerdict(
            tier=tier,
            tool=name,
            advice=advice,
            same_args_failures=counters.same_args_failures,
            tool_failures=counters.tool_failures,
        )

    # -- 查询 / 维护 ---------------------------------------------------------

    def advice_for(self, tool: str) -> Optional[str]:
        """该工具**最近一次失败**的恢复建议（无失败记录 ⇒ ``None``）。

        建议由 ``recovery_advice(tool, last_error)`` 确定性推导——与执行链
        当时写进 metadata 的建议同源，供 ``task_retry`` 终局回喂 /
        ``RetryReplanner`` rationale 使用。
        """
        counters = self._tools.get(str(tool))
        if counters is None or counters.tool_failures <= 0:
            return None
        return recovery_advice(str(tool), counters.last_error)

    def reset(self, tool: Optional[str] = None) -> None:
        """清空单工具（``tool`` 给定）或全部计数（测试与会话边界用）。"""
        if tool is None:
            self._tools.clear()
        else:
            self._tools.pop(str(tool), None)


# ---------------------------------------------------------------------------
# 进程级单例
# ---------------------------------------------------------------------------

_guardrails: Optional[ToolGuardrails] = None


def get_tool_guardrails() -> ToolGuardrails:
    global _guardrails
    if _guardrails is None:
        _guardrails = ToolGuardrails()
    return _guardrails


def reset_tool_guardrails() -> None:
    global _guardrails
    _guardrails = None
