"""P2 ``code_execute`` 工具声明与参数校验（岗位对照·运行时写代码执行）。

职责切分（与 ``task_writes`` / ``plan_mode`` 同款）：

- **声明**（spec + LLM schema）在本文件；
- **实现**（墙内运行时）在 ``agentix/code_exec_runtime.py``（只 import 标准库，
  因为它会被墙内子进程 import）；
- **接线**（注册 handler）在组合根 ``application/services/code_exec_bridge.py``。

本文件还承担本特性的**配置读取**（``CODE_EXEC_*``）：读法用
``getattr(settings, ..., 默认值)`` 而不是 ``settings.CODE_EXEC_ENABLED``——
配置键由配置侧统一落地，读法上留一道默认值兜底，键还没落地时本模块依然
import 得动（flag-off 语义 = 零注册、零行为）。

治理语义：

- 声明级 **L2**（§2.1.5⑤：L2 及以上必填 ``control``）⇒ 有效风险 **HIGH**，
  ``HUMAN_REVIEW_ENABLED=true`` 时必须人工批准（fail-closed）；
- 执行位置强制声明 ``sandbox="subprocess"``：**进程内执行用户代码 = 没有墙**，
  所以这一档不可用时执行点直接拒绝，不静默降级（ADR-0007 ⑮）；
- ``side_effect=True`` + ``retryable=False``：执行用户代码本身即副作用
  （可能写文件、起进程），且 side-effectful 工具禁止自动重试（防重复执行）；
- 工具超时**必须覆盖代码超时的整个取值域**（``timeout_ms = (MAX_TIMEOUT_S + 30) * 1000``）：
  工具超时是治理链的**外层**闸，它若只覆盖当前配置值（缺省 20 秒），调用方传
  ``timeout_s=120`` 时会在代码超时之前被工具侧掐断，用户看到的是"没有理由的失败"。
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, Field

from agentix.code_exec_runtime import (
    DEFAULT_TIMEOUT_S,
    MAX_CODE_CHARS,
    MAX_TIMEOUT_S,
    MIN_TIMEOUT_S,
)
from agentix.config import settings
from agentix.tools.definitions import ToolSpec

__all__ = [
    "CodeExecuteSchema",
    "CODE_EXEC_TOOL_NAME",
    "DEFAULT_MAX_ROUNDS",
    "TOOL_TIMEOUT_MARGIN_S",
    "code_exec_enabled",
    "code_exec_max_rounds",
    "code_exec_timeout_s",
    "validate_code_execute_args",
    "build_code_execute_spec",
]

#: 工具名（声明、执行点、审批词表都按这个名字走）。
CODE_EXEC_TOOL_NAME = "code_execute"

#: 总尝试轮数（**含首次执行**）的缺省值：与 ``CODE_EXEC_MAX_ROUNDS`` 的缺省一致。
DEFAULT_MAX_ROUNDS = 3

#: 工具级超时相对代码级超时的余量（秒）：代码超时先到并给出可解释的
#: ``timed_out=True``，工具级超时只是兜底。
TOOL_TIMEOUT_MARGIN_S = 30


def code_exec_enabled() -> bool:
    """本特性是否开启（缺省关 ⇒ 零注册、逐位等价于基线）。"""
    return bool(getattr(settings, "CODE_EXEC_ENABLED", False))


def code_exec_max_rounds() -> int:
    """总尝试轮数上限（**含首次执行**；缺省 3；至少 1 轮——0 轮等于"一次都不跑"）。"""
    raw = getattr(settings, "CODE_EXEC_MAX_ROUNDS", DEFAULT_MAX_ROUNDS)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_ROUNDS


def code_exec_timeout_s() -> float:
    """代码级超时缺省值（缺省 20 秒；夹到 ``[MIN_TIMEOUT_S, MAX_TIMEOUT_S]``）。"""
    raw = getattr(settings, "CODE_EXEC_TIMEOUT_S", DEFAULT_TIMEOUT_S)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    return max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, value))


class CodeExecuteSchema(BaseModel):
    """``code_execute`` 入参 schema（该工具不进 LLM 可见工具面）。"""

    code: str = Field(
        description=(
            "要执行的 Python 代码正文（必填）。只能使用标准库；"
            "结果必须用 print 输出（工具只取 stdout/stderr）。"
        )
    )
    timeout_s: float = Field(
        # 缺省值也来自配置：写死 20.0 时，配置成 60 秒的部署里模型看到的仍是
        # "默认 20"并照着填 ⇒ 配置被静默忽略。``default_factory`` 在**校验时**
        # 现读配置（配置热切换不必重建模型）。
        default_factory=code_exec_timeout_s,
        ge=1.0,
        le=120.0,
        description=(
            "代码级超时（秒，1~120）。缺省取配置 CODE_EXEC_TIMEOUT_S（缺省 20 秒）；"
            "超时后整个进程组被杀掉。"
        ),
    )


def validate_code_execute_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """校验并规范化 ``code_execute`` 入参；非法输入抛 ``ValueError``。

    **错误信息绝不回显代码正文**（工程约定 6 ③）：代码正文会进日志、
    进轨迹、进模型上下文，把它抄进异常消息等于把它抄进三个本该没有它的位置。
    错误消息里只说"哪一项不合法"和"上限是多少"。

    超时**夹紧**而不拒绝：超时是"最多跑多久"的安全边界，调用方给个 999 只是
    不知道上限——夹住既守住了边界，也不必让模型因为一个数字重试（与
    ``code_exec_runtime._resolve_timeout`` 同口径）。
    """
    code = args.get("code")
    if not isinstance(code, str):
        raise ValueError("code must be a string")
    if not code.strip():
        raise ValueError("code must be a non-empty string")
    if len(code) > MAX_CODE_CHARS:
        raise ValueError(f"code exceeds {MAX_CODE_CHARS} chars")

    raw_timeout = args.get("timeout_s")
    if raw_timeout is None:
        timeout_s = code_exec_timeout_s()
    else:
        try:
            timeout_s = float(raw_timeout)
        except (TypeError, ValueError):
            raise ValueError("timeout_s must be a number") from None
        if timeout_s != timeout_s:  # NaN：比较恒假，先于夹紧挑出来
            raise ValueError("timeout_s must be a number")
        timeout_s = max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, timeout_s))

    return {"code": code, "timeout_s": timeout_s}


def build_code_execute_spec() -> ToolSpec:
    """构造 ``code_execute`` 声明（L2 + control；执行位置 subprocess）。

    工具超时按**代码超时的取值域上界**算，不按当前配置值算：``timeout_s`` 入参
    允许取到 ``MAX_TIMEOUT_S``（120 秒），而工具超时是治理链的**外层**闸——
    它若只覆盖配置值（缺省 20 秒），调用方传 ``timeout_s=120`` 时会在代码超时
    之前被工具侧掐断，用户看到的是"没有理由的失败"。同款口径见 ``cron_run``
    （``timeout_ms`` 覆盖 ``CRON_JOB_TIMEOUT_S`` 上界 + 余量）。
    """
    return ToolSpec(
        name=CODE_EXEC_TOOL_NAME,
        risk_level="L2",  # L2 → HIGH；名称含 execute → HIGH，两层一致
        control=(
            "需人工审批（HITL）：声明级 L2、名称命中 execute ⇒ 有效风险 HIGH，"
            "HUMAN_REVIEW_ENABLED=true 时未获人工批准不得执行（fail-closed，"
            "未获批准时 handler 根本不会被调用）。"
            "执行位置强制为 subprocess 隔离档：独立进程 + RLIMIT_AS/CPU/FSIZE 资源闸，"
            "该档在非 POSIX 平台不可用时执行点直接拒绝，绝不静默退回进程内执行用户代码。"
            "超时按**进程组** SIGKILL（连同被隔离代码 fork 出来的子树一起收）；"
            "被隔离代码只拿到环境白名单（PATH/HOME/TMPDIR/LANG/LC_ALL——"
            "无任何宿主密钥、无 PYTHON*）；"
            "stdout/stderr 有字节与字符双上限，超限即截断并标记。"
        ),
        description=(
            "在隔离沙箱（独立进程 + 资源闸 + 环境白名单）里执行一段 Python 代码，"
            "取回它的 stdout/stderr 与退出码。\n"
            "何时用：只有真的跑一遍才算得出来的问题——数值计算、数据清洗试算、"
            "算法验证、写完一段代码后自测。\n"
            "何时不能用：需要访问网络、需要读写宿主文件、需要安装第三方依赖、"
            "或需要长时间运行的任务（本工具只跑在 POSIX 的 subprocess 隔离档里，"
            "平台不支持时直接拒绝执行）；"
            "以及只是问事实、查知识库这类不需要执行的问题。\n"
            "注意：HIGH 风险工具——只能使用标准库、必须用 print 输出结果、"
            "超时会连整个进程组一起杀掉、输出有体积上限（超出会被截断）。"
        ),
        parameters=CodeExecuteSchema.model_json_schema(),
        side_effect=True,  # 执行用户代码即副作用：禁止自动重试（防重复执行）
        capabilities=("code",),
        sandbox="subprocess",  # 强制隔离档：进程内执行用户代码等于没有墙
        retryable=False,
        # 工具超时必须覆盖代码超时的**整个取值域**（不是只覆盖当前配置值）：
        # 代码超时先到并给出可解释的 ``timed_out=True``，工具级超时只是兜底。
        timeout_ms=int((MAX_TIMEOUT_S + TOOL_TIMEOUT_MARGIN_S) * 1000),
    )
