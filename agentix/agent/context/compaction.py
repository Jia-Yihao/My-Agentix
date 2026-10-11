"""L4 语义压缩（Phase 22，Ch2 §2.7 分层压缩最后一档）。

仅当 L1–L3 组装后仍超预算时作为**最后手段**触发
（``CONTEXT_AUTOCOMPACT`` 默认 ``False`` 关闭）。职责：

- **五要素机制断言**：trace_id、错误记录、约束 key、来源引用、任务状态
  字段在压缩前后必须原样保留（token 级比对，正文可缩写）；
- **单次尝试 + 确定性回退**：压缩器异常 / 空输出 / 五要素缺失 ⇒ 返回
  ``None``，调用方走 Phase 18 丢弃循环——离线 record/eval 不依赖 LLM。

P3（岗位对照·渐进上下文，借鉴 claw-code 渐进压缩重试梯）：
``CONTEXT_COMPACT_LADDER=True``（默认 False）时单次尝试升级为
keep-last 窗口阶梯 4→2→1→0——每轮只压缩头部、尾部原样保留（最新
上下文不参与压缩），空输出/五要素被拒 ⇒ 换更小的 keep-last 窗口再试；
**压缩器异常立即回退**（provider 故障换窗口无意义）；四轮耗尽 ⇒
``None`` 回退丢弃循环（确定性终止）。flag-off = Phase 22 单次语义
逐字节不变。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

from loguru import logger

from agentix.agent.context.token_count import count_tokens
from agentix.domain.ports import CompactorPort

__all__ = [
    "FIVE_ELEMENTS",
    "COMPACT_RULES",
    "COMPACT_LADDER_ROUNDS",
    "PRECOMPACT_RULE_MARK",
    "PRECOMPACT_CONSTRAINT_MARK",
    "PRECOMPACT_REINJECT_MAX_CHARS",
    "missing_elements",
    "autocompact_enabled",
    "ladder_enabled",
    "precompact_reinject_enabled",
    "build_reinject_block",
    "maybe_compact",
]

#: 五要素（Ch2 §2.7）——每类一组「压缩后必须原样保留」的 token 模式。
#: 只匹配**正则无捕获组**的完整 token，保证 findall 输出可直接做集合比对。
FIVE_ELEMENTS: Dict[str, "re.Pattern[str]"] = {
    "trace_id": re.compile(r"trace[_-]?id[=:]\s*\S+", re.IGNORECASE),
    "error_record": re.compile(
        r"\[[^\s\]]*\|fail\]|\berror_code[=:]\s*\S+", re.IGNORECASE
    ),
    "constraint_key": re.compile(
        r"\bconstraint[=:]\s*\S+|\[c:[^\]]+\]", re.IGNORECASE
    ),
    "source_citation": re.compile(
        r"obs://\S+|【[^】]+】|\[(?:source|ref)[=:][^\]]+\]", re.IGNORECASE
    ),
    "task_status": re.compile(r"\b(?:status|state)[=:]\s*\S+", re.IGNORECASE),
}

#: 压缩 prompt 的硬约束（生产 LLMCompactor 使用；与机制断言同源双保险）。
COMPACT_RULES = (
    "Compress the following agent trajectory to fit the token budget. "
    "You MUST preserve verbatim every occurrence of: (1) trace ids "
    "(trace_id=...), (2) error records ([step|fail] markers, error_code=...), "
    "(3) constraint keys (constraint=..., [c:...]), (4) source citations "
    "(obs://..., 【...】, source=/ref=...), (5) task status fields "
    "(status=..., state=...). Condense prose only. "
    "Return the compressed trajectory text and nothing else."
)


def missing_elements(original: str, compacted: str) -> Dict[str, List[str]]:
    """五要素机制断言：``original`` 中出现的 token 必须在 ``compacted`` 保留。

    返回 ``{要素类: [丢失 token, ...]}``；空 dict = 通过。原文中某类要素
    从未出现 ⇒ 该类空真通过（正文可缩写，不凭空要求）。
    """
    missing: Dict[str, List[str]] = {}
    for key, pattern in FIVE_ELEMENTS.items():
        orig_tokens = list(dict.fromkeys(pattern.findall(original)))
        if not orig_tokens:
            continue
        kept = set(pattern.findall(compacted))
        lost = [t for t in orig_tokens if t not in kept]
        if lost:
            missing[key] = lost
    return missing


def autocompact_enabled() -> bool:
    """懒读 flag——每次构建时判定，运行中改配置即时生效；故障 ⇒ 关闭。"""
    try:
        from agentix.config.settings import settings

        return bool(settings.CONTEXT_AUTOCOMPACT)
    except Exception:  # noqa: BLE001 - 配置读取失败不得拖垮上下文构建
        return False


#: P3：渐进压缩阶梯的 keep-last 窗口序列（借鉴 claw-code 4→2→1→0；
#: 每轮尾部原样保留，压缩面逐步扩大）。
COMPACT_LADDER_ROUNDS: Tuple[int, ...] = (4, 2, 1, 0)


def ladder_enabled() -> bool:
    """懒读阶梯 flag——故障 ⇒ 关闭（单次尝试语义）。"""
    try:
        from agentix.config.settings import settings

        return bool(getattr(settings, "CONTEXT_COMPACT_LADDER", False))
    except Exception:  # noqa: BLE001 - 配置读取失败不得拖垮上下文构建
        return False


# ---------------------------------------------------------------------------
# F2a·M-P2-2（memory-optimization §6 PreCompact 重注）：压缩前把「关键规则 +
# 用户硬约束」预采集，重注到上下文尾部高注意力区——解决长会话规则丢失。
# 行携带 `[precompact:<source>]` 标记 ⇒ 规则来源逐行可审计。
# ---------------------------------------------------------------------------

#: 重注行审计标记（source ∈ rule | constraint——规则来源可审计）。
PRECOMPACT_RULE_MARK = "[precompact:rule]"
PRECOMPACT_CONSTRAINT_MARK = "[precompact:constraint]"

#: 重注块总字符上限：约束优先、规则按声明序补满；有界 ⇒ token 影响有界
#: （超预算时块计入每次 prompt 估算，丢弃循环照常收敛）。
PRECOMPACT_REINJECT_MAX_CHARS = 1500


def precompact_reinject_enabled() -> bool:
    """懒读重注 flag——故障 ⇒ 关闭（与基线逐字节一致）。"""
    try:
        from agentix.config.settings import settings

        return bool(getattr(settings, "CONTEXT_PRECOMPACT_REINJECT_ENABLED", False))
    except Exception:  # noqa: BLE001 - 配置读取失败不得拖垮上下文构建
        return False


def build_reinject_block(
    rule_lines: Sequence[str],
    traj_lines: Sequence[str],
    *,
    max_chars: int = PRECOMPACT_REINJECT_MAX_CHARS,
) -> Tuple[List[str], Dict[str, int]]:
    """M-P2-2：压缩**前**采集「关键规则 + 用户硬约束」→ 尾部重注块（纯函数）。

    - **约束优先**：先从原轨迹抽取五要素 ``constraint_key`` token（用户硬
      约束，去重按出现序）——正文可丢、约束必须在高注意力区再现；
    - 规则源 = 调用方渲染好的规则行（``ContextEngine`` 的 ``RULE ...`` /
      ``SimpleContextBuilder`` 的原样规则），按声明序补满剩余额度；
    - ``max_chars`` 截断（首行超限即止）；两类皆空 ⇒ 空块；
    - 返回 ``(块行, 审计计数)``，计数键 ``rule``/``constraint`` 为各来源
      入块行数（调用方落 snapshot meta ⇒ 来源可审计）。
    """
    block: List[str] = []
    counts: Dict[str, int] = {"rule": 0, "constraint": 0}
    used = 0

    def _add(row: str, source: str) -> bool:
        nonlocal used
        if used + len(row) + 1 > max_chars:
            return False
        block.append(row)
        used += len(row) + 1
        counts[source] += 1
        return True

    joined = "\n".join(str(line) for line in traj_lines)
    seen: set = set()
    for token in FIVE_ELEMENTS["constraint_key"].findall(joined):
        if token in seen:
            continue
        seen.add(token)
        if not _add(f"{PRECOMPACT_CONSTRAINT_MARK} {token}", "constraint"):
            break
    for line in rule_lines:
        text = str(line).strip()
        if not text:
            continue
        if not _add(f"{PRECOMPACT_RULE_MARK} {text}", "rule"):
            break
    return block, counts


async def _ladder_compact(
    traj_lines: Sequence[str],
    compactor: CompactorPort,
    budget_tokens: int,
) -> Optional[List[str]]:
    """keep-last 窗口阶梯压缩（仅 ``CONTEXT_COMPACT_LADDER=True`` 走此路径）。

    - 每轮只压缩头部 ``traj_lines[:-keep]``，尾部 ``keep`` 行原样拼接——
      最新上下文不进压缩面，信息损失有界；
    - 空输出 / 五要素被拒 ⇒ 下一轮（更小的 keep-last 窗口）；
    - **压缩器异常 ⇒ 立即 ``None``**（provider 故障换窗口无意义）；
    - 本仍超预算且 keep>0 ⇒ 扩大压缩面再试；keep=0 轮接受后交还调用方
      的丢弃循环收尾（确定性终止）；
    - 四轮耗尽无一通过 ⇒ ``None``。
    """
    for keep in COMPACT_LADDER_ROUNDS:
        if keep and len(traj_lines) <= keep:
            continue  # 头部为空：无可压缩面，直接换更小窗口
        source = list(traj_lines[:-keep]) if keep else list(traj_lines)
        tail = list(traj_lines[len(traj_lines) - keep :]) if keep else []
        text = "\n".join(source)
        try:
            out = await compactor.compact(text, budget_tokens=budget_tokens)
        except Exception as exc:  # noqa: BLE001 - L4 不可用 ⇒ 丢弃回退
            logger.warning(
                "L4 ladder compaction failed; falling back to drop-loop: {}", exc
            )
            return None
        if not isinstance(out, str) or not out.strip():
            logger.warning(
                "L4 ladder round empty (keep_last={}); trying narrower window", keep
            )
            continue
        missing = missing_elements(text, out)
        if missing:
            logger.warning(
                "L4 ladder round rejected (keep_last={}); missing={}",
                keep,
                sorted(missing),
            )
            continue
        result = "\n".join([out.rstrip("\n"), *tail]) if tail else out
        if keep == 0 or count_tokens(result) <= budget_tokens:
            return result.splitlines()
        logger.debug(
            "L4 ladder round still over budget (keep_last={}); widening window", keep
        )
    return None


async def maybe_compact(
    traj_lines: Sequence[str],
    compactor: Optional[CompactorPort],
    budget_tokens: int,
) -> Optional[List[str]]:
    """L4 压缩尝试：单次（默认）或 keep-last 阶梯（``CONTEXT_COMPACT_LADDER``）。

    任何失败（compactor 缺失 / flag 关 / 抛异常 / 空输出 / 五要素缺失）
    ⇒ ``None``，调用方回退 Phase 18 丢弃循环——保证确定性终止。
    """
    if compactor is None or not traj_lines or not autocompact_enabled():
        return None
    if ladder_enabled():
        return await _ladder_compact(traj_lines, compactor, budget_tokens)
    original = "\n".join(traj_lines)
    try:
        compacted = await compactor.compact(original, budget_tokens=budget_tokens)
    except Exception as exc:  # noqa: BLE001 - L4 不可用 ⇒ 丢弃回退
        logger.warning("L4 compaction failed; falling back to drop-loop: {}", exc)
        return None
    if not isinstance(compacted, str) or not compacted.strip():
        logger.warning("L4 compaction returned empty output; falling back to drop-loop")
        return None
    missing = missing_elements(original, compacted)
    if missing:
        logger.warning(
            "L4 compaction rejected by five-element assertion; missing={}",
            sorted(missing),
        )
        return None
    return compacted.splitlines()
