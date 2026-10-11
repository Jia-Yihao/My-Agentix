"""Prompt 静态前缀编排保证（**框架级机制**，非逐点守卫）。

《My Agentix_架构优化方案》§9.1 C 组缺口「System Prompt/工具定义在静态前缀」：
现状 —— 静态前缀违规**已逐点修掉**（批次 6/7 处理了 4 处审计点、归一了 3 处
重复前缀串，并有 `tests/test_p23::TestPromptPrefixSingleSource` 守着），
但该条仍标 ⚠️ 的唯一理由是：

> **无全局「静态在前 / 动态在后」的编排保证**——依赖逐点守卫而非框架级机制。

**为什么逐点守卫不够**：每新增一个 prompt builder，就新增一个可能把动态内容
（用户输入、检索结果、当前时间）插到静态区之前的机会。逐点守卫**只覆盖已
写过的点**，对未来的新点没有约束力。

**本模块提供两层框架级保证**：

1. :class:`PromptEnvelope` —— 把「静态前缀 + 动态后缀」变成**类型**：
   ``render()`` 的拼接顺序由类型保证，写不出「动态在前」的代码。
2. :func:`assert_static_prefix_stable` —— **不变性验证**：用多组**不同的**
   动态输入渲染同一个 builder，断言它们的公共前缀长度 ≥ 声明的静态常量。
   这把「静态区真的没被动态内容污染」从**人工审查**变成**可执行断言**。

第 2 点是关键：它不检查「代码长什么样」，而是检查「**输出是否真的不变**」——
即使有人用很隐蔽的方式（条件分支、隐式拼接）把动态内容混进静态区，只要
输出随输入变化，断言就会失败。

**零依赖**：仅 stdlib + typing，可被 domain / application / infrastructure 任意层使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Sequence, Tuple

__all__ = [
    "PromptEnvelope",
    "StaticPrefixViolation",
    "assemble",
    "assert_static_prefix_stable",
    "common_prefix_len",
]

#: 判定「静态前缀稳定」时，公共前缀至少要覆盖的字符数（低于此值视为无静态区）
MIN_STATIC_PREFIX_CHARS = 40


class StaticPrefixViolation(AssertionError):
    """静态前缀不变性被破坏（动态内容混入静态区 / 前缀过短）。"""


@dataclass(frozen=True)
class PromptEnvelope:
    """「静态前缀 + 动态后缀」的不可变载体。

    拼接顺序由 :meth:`render` **唯一**决定（``static + dynamic``），
    因此调用方**无法**写出「动态在前」的 prompt——这是类型层面的保证，
    不依赖代码审查。

    ``static_prefix`` 应当是模块级常量（便于 :func:`assert_static_prefix_stable`
    断言公共前缀恰为它），``dynamic_suffix`` 可含用户输入 / 检索结果 / 时间。
    """

    static_prefix: str
    dynamic_suffix: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.static_prefix, str):
            raise StaticPrefixViolation("static_prefix 必须是 str")
        if not isinstance(self.dynamic_suffix, str):
            raise StaticPrefixViolation("dynamic_suffix 必须是 str")

    def render(self) -> str:
        """按「静态在前、动态在后」渲染。"""
        return self.static_prefix + self.dynamic_suffix

    @property
    def static_chars(self) -> int:
        return len(self.static_prefix)

    @property
    def is_pure_static(self) -> bool:
        return not self.dynamic_suffix

    def to_dict(self) -> Dict[str, Any]:
        return {
            "static_chars": self.static_chars,
            "dynamic_chars": len(self.dynamic_suffix),
            "is_pure_static": self.is_pure_static,
        }


def assemble(static_prefix: str, dynamic_suffix: str = "") -> PromptEnvelope:
    """构造信封（便捷函数，语义与直接构造一致）。"""
    return PromptEnvelope(static_prefix=static_prefix, dynamic_suffix=dynamic_suffix)


def common_prefix_len(texts: Sequence[str]) -> int:
    """多段文本的**公共前缀长度**（空输入为 0）。"""
    if not texts:
        return 0
    first = texts[0]
    limit = min(len(t) for t in texts)
    idx = 0
    while idx < limit and all(t[idx] == first[idx] for t in texts):
        idx += 1
    return idx


def assert_static_prefix_stable(
    build: Callable[[Any], str],
    samples: Iterable[Any],
    *,
    min_prefix_chars: int = MIN_STATIC_PREFIX_CHARS,
    expected_prefix: str | None = None,
    label: str = "<builder>",
) -> str:
    """断言 ``build`` 的输出具有**稳定的静态前缀**（不变性验证）。

    :param build: 接收一个样本、返回**完整 prompt 字符串**的构造器。
    :param samples: 至少 2 组**不同**的动态输入；差异越大越能暴露污染。
    :param min_prefix_chars: 公共前缀至少要有多少字符（否则说明静态区太薄）。
    :param expected_prefix: 若给出，公共前缀必须**以它开头**（即声明的静态常量
        确实是输出开头）。
    :return: 实测公共前缀（便于调用方进一步断言）。

    :raises StaticPrefixViolation: 样本不足 / 前缀过短 / 与声明不符。
    """
    materialized: List[Any] = list(samples)
    if len(materialized) < 2:
        raise StaticPrefixViolation(f"{label}: 至少需要 2 组样本才能验证前缀稳定性")

    rendered: List[str] = []
    for sample in materialized:
        out = build(sample)
        if not isinstance(out, str):
            raise StaticPrefixViolation(f"{label}: builder 必须返回 str，实际 {type(out).__name__}")
        rendered.append(out)

    if len(set(rendered)) < 2:
        # 所有样本产出相同 ⇒ 这个 builder 的「动态」其实没生效，验证没有意义
        raise StaticPrefixViolation(
            f"{label}: 不同样本产出完全相同的 prompt——动态部分未生效，无法验证前缀稳定性"
        )

    prefix_len = common_prefix_len(rendered)
    if prefix_len < int(min_prefix_chars):
        raise StaticPrefixViolation(
            f"{label}: 公共前缀仅 {prefix_len} 字符（要求 ≥ {min_prefix_chars}）——"
            f"动态内容可能混入了静态区，或静态区过薄"
        )

    measured = rendered[0][:prefix_len]
    if expected_prefix is not None and not measured.startswith(expected_prefix):
        raise StaticPrefixViolation(
            f"{label}: 公共前缀与声明的静态常量不符\n"
            f"  实测前 80 字符: {measured[:80]!r}\n"
            f"  声明静态常量  : {expected_prefix[:80]!r}"
        )
    return measured


def envelope_report(envelopes: Sequence[PromptEnvelope]) -> List[Tuple[int, int]]:
    """批量摘要：返回 ``[(static_chars, dynamic_chars), ...]``（便于快照测试）。"""
    return [(e.static_chars, len(e.dynamic_suffix)) for e in envelopes]
