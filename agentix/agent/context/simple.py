"""Context 组装（Phase 2 最小实现；Phase 5 扩展稳定前缀/预算/状态栏）。

当前：prompt = [静态规则前缀] + goal + 状态栏 meta；token_estimate 用粗糙 len/4。

Phase 18 增量（默认零行为变化）：

- 状态栏白名单过滤（与 ``ContextEngine`` 同一信任源——状态不得模型自写）；
- ``rules``：指令分层注入（``instructions.build_instructions`` 的输出；
  默认 ``()`` = 静态前缀为空，prompt 与既有逐字节一致）。
"""

from __future__ import annotations

from typing import Optional, Sequence

from agentix.agent.context.compaction import (
    autocompact_enabled,
    build_reinject_block,
    maybe_compact,
    precompact_reinject_enabled,
)
from agentix.agent.context.engine import STATUS_BAR_TRUSTED_KEYS
from agentix.agent.context.token_count import count_tokens
from agentix.domain.agent.types import ContextSnapshot, Observation, TaskSpec
from agentix.domain.ports import CompactorPort

__all__ = ["SimpleContextBuilder"]

#: 段级配额外置观测保留的头部字符数（与 ContextEngine 同值）
_SEGMENT_ISOLATE_HEAD = 200


class SimpleContextBuilder:
    def __init__(
        self,
        *,
        status_bar: dict | None = None,
        rules: Sequence[str] = (),
        compactor: Optional[CompactorPort] = None,
    ) -> None:
        raw = dict(status_bar or {})
        # Phase 18：状态栏白名单（状态不得由模型自写；与 ContextEngine 同源）
        self._status_bar = {k: raw[k] for k in raw if k in STATUS_BAR_TRUSTED_KEYS}
        self._rules = tuple(str(r) for r in rules if str(r).strip())
        self._compactor = compactor

    async def build(
        self,
        task: TaskSpec,
        *,
        trajectory: Sequence[Observation] = (),
        budget_tokens: int = 0,
    ) -> ContextSnapshot:
        static = "\n".join(self._rules)
        # 段级输出预算（TOOL_OUTPUT_BUDGET_TOKENS，默认 0=关）：
        # 与 ContextEngine 同语义——超配额观测整体外置为 obs:// 可取回引用
        # （复用既有 _isolate_ref / ArtifactStore 通道）而非截断；flag-off
        # 恒不计数，轨迹行与基线逐字节等价。
        from agentix.config.settings import settings
        from agentix.agent.context.engine import _isolate_ref

        seg_budget = max(0, int(getattr(settings, "TOOL_OUTPUT_BUDGET_TOKENS", 0) or 0))
        seg_used = 0
        isolated: list[str] = []
        traj_lines: list[str] = []
        for o in trajectory:
            body = o.content
            seg_cost = count_tokens(body) if seg_budget else 0
            if seg_budget and seg_used + seg_cost > seg_budget:
                ref, body = _isolate_ref(o.step_id, body, _SEGMENT_ISOLATE_HEAD)
                isolated.append(ref)
            elif seg_budget:
                seg_used += seg_cost
            traj_lines.append(f"[{o.step_id}] {body}")

        # F2a·M-P2-2 PreCompact 重注（与 ContextEngine 同语义）：超预算且
        # （重注 flag ∧ CONTEXT_AUTOCOMPACT）⇒ 压缩前采集规则+硬约束块挂到
        # 尾部；flag-off 块恒空 ⇒ assemble 与 Phase 18 逐字节一致。
        reinject_block: list = []
        reinject_counts: dict = {}

        def assemble(lines: Sequence[str]) -> str:
            body = task.goal + ("\n" + "\n".join(lines) if lines else "")
            prompt = (static + "\n" + body) if static else body
            if reinject_block:
                prompt = prompt + "\n" + "\n".join(reinject_block)
            return prompt

        prompt = assemble(traj_lines)
        # 确定性截断：粗估 token；budget_tokens>0 时从尾部丢弃轨迹行
        truncated = False
        if (
            budget_tokens > 0
            and traj_lines
            and count_tokens(prompt) > budget_tokens
            and precompact_reinject_enabled()
            and autocompact_enabled()
        ):
            reinject_block, reinject_counts = build_reinject_block(
                list(self._rules), traj_lines
            )
            prompt = assemble(traj_lines)
        # Phase 22（L4）：L1–L3 组装后仍超预算 ⇒ 语义压缩（最后手段）。
        # flag 关（默认）或 compactor 未注入 ⇒ maybe_compact 立即 None，
        # 行为与 Phase 18 逐字节一致。
        if budget_tokens > 0 and traj_lines and count_tokens(prompt) > budget_tokens:
            compacted = await maybe_compact(traj_lines, self._compactor, budget_tokens)
            if compacted is not None:
                traj_lines = compacted
                truncated = True
                prompt = assemble(traj_lines)
        if budget_tokens > 0:
            while traj_lines and count_tokens(prompt) > budget_tokens:
                traj_lines.pop()
                truncated = True
                prompt = assemble(traj_lines)
        return ContextSnapshot(
            prompt=prompt,
            token_estimate=max(1, count_tokens(prompt)),
            truncated=truncated,
            status_bar=dict(self._status_bar),
            meta=(
                dict(
                    {"precompact_reinject": dict(reinject_counts)}
                    if reinject_block else {}
                )
                | ({"isolated_refs": isolated} if isolated else {})
            ),
        )
