"""Context 组装（Phase 5）：Static Prefix + Trajectory、状态栏信任源、确定性截断。

隔离优于压缩（书 §2.9）：超大块不内联进 prompt，放入 ``meta['isolated_refs']``。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from agentix.agent.context.compaction import (
    autocompact_enabled,
    build_reinject_block,
    maybe_compact,
    precompact_reinject_enabled,
)
from agentix.agent.context.token_count import count_tokens
from agentix.domain.agent.types import ContextSnapshot, Observation, TaskSpec
from agentix.domain.ports import CompactorPort

__all__ = ["ContextEngine", "STATUS_BAR_TRUSTED_KEYS"]

#: Phase H·W1：段级配额外置观测保留的头部字符数（小头部 + obs:// 指针）
_SEGMENT_ISOLATE_HEAD = 200

#: 状态栏只允许这些 key（状态不得由模型自写——白名单校验）
STATUS_BAR_TRUSTED_KEYS: frozenset = frozenset(
    {
        "route",
        "tenant_id",
        "kb_id",
        "session_id",
        "scope",
        "案件阶段",
        "recall_count",
        "mode",
        "risk_max",
        # Phase 28（Ch10 §10.6 预算意识）：预算上限**常量**键（flag-gated 写入）。
        # 只允许常量——状态栏位于静态前缀，动态剩余量（每步变化）会打穿 KV Cache；
        # 动态预算走轨迹 budget_notice（append-only），见 application/services/budget_notice.py。
        "max_iter",
    }
)


def _isolate_ref(
    step_id: str, body: str, keep_chars: int
) -> "tuple[str, str]":
    """超长观测隔离：返回 ``(隔离引用, 进 prompt 的头部片段)``。

    - **flag-off（默认）**：``obs://{step_id}`` 裸引用 + 头部——与基线
      逐字节一致（引用是悬空的，本函数只在 flag-on 时把它变可取回）；
    - **flag-on（M-P1-5 obs:// 接线）**：正文经 ``offload_content`` 真正
      外置 ArtifactStore，引用落 ``obs://{artifact_id}``——经
      ``read_artifact``（obs:// 前缀自动剥离）可取回，与记忆索引指针共用
      同一条取回通道；
    - **fail-open**：外置失败/降级 ⇒ 回裸引用行为，隔离本身绝不抛错。
    """
    from agentix.config.settings import settings

    if not bool(getattr(settings, "CONTEXT_ISOLATE_PERSIST_ENABLED", False)):
        return (
            f"obs://{step_id}",
            body[:keep_chars] + f"\n…[isolated obs://{step_id}]",
        )
    try:
        from agentix.application.services.artifact_store import offload_content

        result = offload_content(
            body,
            kind="agent_step",
            inline_threshold=1,  # 隔离语义 = 强制外置（进入本分支必超长）
            rebuildable=True,
            meta={"step_id": step_id},
        )
    except Exception as exc:  # noqa: BLE001 - 外置失败回裸引用，不打断组装
        from loguru import logger

        logger.warning("isolate persist failed (fallback to bare ref): {}", exc)
        result = None
    if result is not None and result.is_offloaded and result.ref is not None:
        artifact_id = result.ref.artifact_id
        return (
            f"obs://{artifact_id}",
            body[:keep_chars] + f"\n…[isolated obs://{artifact_id}]",
        )
    return (
        f"obs://{step_id}",
        body[:keep_chars] + f"\n…[isolated obs://{step_id}]",
    )


class ContextEngine:
    """实现 ContextPort。

    - ``static_prefix``：状态栏（排序、白名单过滤）+ 系统约束行；
    - trajectory：观测按序追加（Append-only 语义）；
    - ``budget_tokens``：粗估 len/4；超限裁剪策略由 ``trim_policy`` 决定：

      - ``"tail"``（默认，现状）：从**轨迹尾部**（最新行）丢弃——前缀保持稳定，
        适配 prompt 缓存纪律；
      - ``"head"``（L2 Snip，按龄）：从**轨迹头部**（最旧行）丢弃——保留最近
        上下文，适合长会话时间深度裁剪；``GOAL`` 与静态前缀永不丢弃。

    - 大行放 ``isolated_refs``（artifact 通道），不进 prompt。
    """

    _TRIM_POLICIES = ("tail", "head")

    def __init__(
        self,
        *,
        status_bar: Optional[Dict[str, Any]] = None,
        system_rules: Sequence[str] = (),
        isolate_over_chars: int = 4000,
        trim_policy: str = "tail",
        compactor: Optional[CompactorPort] = None,
    ) -> None:
        if trim_policy not in self._TRIM_POLICIES:
            raise ValueError(
                f"invalid trim_policy '{trim_policy}' (must be one of {self._TRIM_POLICIES})"
            )
        raw = dict(status_bar or {})
        self._status_bar = {k: raw[k] for k in sorted(raw) if k in STATUS_BAR_TRUSTED_KEYS}
        self._system_rules = tuple(system_rules)
        self._isolate_over = max(0, isolate_over_chars)
        self._trim_policy = trim_policy
        self._compactor = compactor

    def _static_prefix(self) -> str:
        lines: List[str] = []
        if self._status_bar:
            lines.append("STATUS " + " ".join(f"{k}={self._status_bar[k]}" for k in sorted(self._status_bar)))
        for rule in self._system_rules:
            lines.append(f"RULE {rule}")
        return "\n".join(lines)

    async def build(
        self,
        task: TaskSpec,
        *,
        trajectory: Sequence[Observation] = (),
        budget_tokens: int = 0,
    ) -> ContextSnapshot:
        prefix = self._static_prefix()
        isolated: List[str] = []
        traj_lines: List[str] = []
        # Phase H·W1 段级输出预算（TOOL_OUTPUT_BUDGET_TOKENS，默认 0=关）：
        # 轨迹段（工具/检索观测）累计超过配额的观测**整体外置**为 obs://
        # 可取回引用（复用既有 _isolate_ref / ArtifactStore 通道）而非截断丢
        # 数据；flag-off 恒不计数、循环与基线逐字节等价。
        from agentix.config.settings import settings

        seg_budget = max(0, int(getattr(settings, "TOOL_OUTPUT_BUDGET_TOKENS", 0) or 0))
        seg_used = 0
        for o in trajectory:
            body = o.content
            seg_cost = count_tokens(body) if seg_budget else 0
            if self._isolate_over and len(body) > self._isolate_over:
                ref, body = _isolate_ref(o.step_id, body, self._isolate_over)
                isolated.append(ref)
            elif seg_budget and seg_used + seg_cost > seg_budget:
                # 段级配额：外置后 prompt 只留小头部 + 可取回指针
                ref, body = _isolate_ref(o.step_id, body, _SEGMENT_ISOLATE_HEAD)
                isolated.append(ref)
            elif seg_budget:
                seg_used += seg_cost
            flag = "ok" if o.ok else "fail"
            traj_lines.append(f"[{o.step_id}|{flag}] {body}")

        # F2a·M-P2-2 PreCompact 重注：先按无块状态组装一次；一旦超预算且
        # （重注 flag ∧ CONTEXT_AUTOCOMPACT 挂通道）⇒ **压缩前**从原轨迹采集
        # 规则+硬约束块挂到尾部（高注意力区）。flag-off/未超预算 ⇒ 块恒空，
        # assemble 与基线逐字节等价；块不在 traj_lines 内（丢弃循环不删它，
        # 规则必须幸存），但计入每次 prompt 的 token 估算（丢弃循环照常收敛）。
        reinject_block: List[str] = []
        reinject_counts: Dict[str, int] = {}

        def assemble(lines: List[str]) -> str:
            parts = [prefix] if prefix else []
            parts.append(f"GOAL {task.goal}")
            parts.extend(lines)
            if reinject_block:
                parts.extend(reinject_block)
            return "\n".join(parts)

        prompt = assemble(traj_lines)
        truncated = False
        if (
            budget_tokens > 0
            and traj_lines
            and count_tokens(prompt) > budget_tokens
            and precompact_reinject_enabled()
            and autocompact_enabled()
        ):
            reinject_block, reinject_counts = build_reinject_block(
                [f"RULE {rule}" for rule in self._system_rules], traj_lines
            )
            prompt = assemble(traj_lines)
        # Phase 22（L4，Ch2 §2.7）：L1–L3 组装后仍超预算 ⇒ 语义压缩（最后手段）。
        # flag 关（默认）或 compactor 未注入 ⇒ maybe_compact 立即返回 None，
        # 后续丢弃循环与 Phase 18 逐字节一致。
        if budget_tokens > 0 and traj_lines and count_tokens(prompt) > budget_tokens:
            compacted = await maybe_compact(traj_lines, self._compactor, budget_tokens)
            if compacted is not None:
                traj_lines = compacted
                truncated = True
                prompt = assemble(traj_lines)
        if budget_tokens > 0:
            while traj_lines and count_tokens(prompt) > budget_tokens:
                if self._trim_policy == "head":
                    traj_lines.pop(0)  # L2 Snip：按龄丢最旧行（保最近）
                else:
                    traj_lines.pop()  # 现状：丢最新行（前缀稳定）
                truncated = True
                prompt = assemble(traj_lines)

        meta: Dict[str, Any] = {}
        if isolated:
            meta["isolated_refs"] = isolated
        if reinject_block:
            # F2a·M-P2-2 审计：各来源入块行数（来源可审计）
            meta["precompact_reinject"] = dict(reinject_counts)
        return ContextSnapshot(
            prompt=prompt,
            token_estimate=max(1, count_tokens(prompt)),
            truncated=truncated,
            status_bar=dict(self._status_bar),
            meta=meta,
        )
